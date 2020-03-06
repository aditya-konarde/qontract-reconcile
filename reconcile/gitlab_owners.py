import base64
import json
import logging
import pathlib

from dateutil import parser as dateparser

from ruamel import yaml

from reconcile import queries
from utils.gitlab_api import GitLabApi
from utils.gitlab_api import MRState


QONTRACT_INTEGRATION = 'gitlab-owners'

APPROVAL_LABEL = 'approved'

_LOG = logging.getLogger(__name__)


class ProjectOwners:
    """
    Abstracts the owners of a project with per-path granularity.
    """

    def __init__(self, gitlab_client):
        self._gitlab = gitlab_client
        self._owners_map = self._get_owners_map()

    def get_all_owners(self, path):
        """
        Gets all the owners of a given path, no matter in which
        level of the filesystem tree the owner was specified.
        """
        path_owners = set()
        for owned_path, owners in self._owners_map.items():
            if path.startswith(owned_path):
                path_owners.update(owners)
        if not path_owners:
            raise KeyError(f'No owners for path {path!r}')
        # Returns a sorted list of unique owners
        return sorted(path_owners)

    def get_closest_owners(self, path):
        """
        Gets all closest owners of a given path, no matter in which
        level of the filesystem tree the owner was specified.
        """
        candidates = []

        for owned_path in self._owners_map:
            if path.startswith(owned_path):
                candidates.append(owned_path)

        if not candidates:
            raise KeyError(f'No owners for path {path!r}')

        # The longest owned_path is the chosen
        elected = max(candidates, key=lambda x: len(x))
        # Returns a sorted list of unique owners
        return sorted(set(self._owners_map[elected]))

    def _get_owners_map(self):
        """
        Maps all the OWNERS files content to their respective
        owned directory.
        """
        owners_map = dict()
        repo_tree = self._gitlab.get_repository_tree(ref='master')
        for item in repo_tree:
            if item['name'] != 'OWNERS':
                continue
            if item['type'] != 'blob':
                continue
            file_info = self._gitlab.project.repository_blob(item['id'])
            content = base64.b64decode(file_info['content']).decode()
            owners_path = str(pathlib.Path(item['path']).parent)
            try:
                approvers = yaml.safe_load(content)['approvers']
            except KeyError:
                _LOG.debug(f'Not able to load the approvers from '
                           f'"{item["path"]}"')
                continue
            owners_map[owners_path] = approvers
        return owners_map


class OwnerNotFoundError(Exception):
    """
    Used when an owner is not found for a change.
    """


class MRApproval:
    """
    Processes a Merge Request, looking for matches
    between the approval messages the the project owners.
    """
    def __init__(self, gitlab_client, merge_request, owners, dry_run):
        self.gitlab = gitlab_client
        self.mr = merge_request
        self.owners = owners
        self.dry_run = dry_run

        top_commit = next(self.mr.commits())
        self.top_commit_created_at = dateparser.parse(top_commit.created_at)

    def get_change_owners_map(self):
        """
        Maps each change path to the list of owners that can approve
        that change.
        """
        change_owners_map = dict()
        paths = self.gitlab.get_merge_request_changed_paths(self.mr.iid)
        for path in paths:
            try:
                change_owners_map[path] = \
                    {
                        'owners': self.owners.get_all_owners(path),
                        'closest_owners': self.owners.get_closest_owners(path)
                    }
            except KeyError as exception:
                raise OwnerNotFoundError(exception)
        return change_owners_map

    def get_lgtms(self):
        """
        Collects the usernames of all the '/lgtm' comments.
        """
        lgtms = []
        comments = self.gitlab.get_merge_request_comments(self.mr.iid)
        for comment in comments:

            # Only interested in '/lgtm' comments
            if comment['body'] != '/lgtm':
                continue

            # Only interested in comments created after the top commit
            # creation time
            comment_created_at = dateparser.parse(comment['created_at'])
            if comment_created_at < self.top_commit_created_at:
                continue

            lgtms.append(comment['username'])
        return lgtms

    def get_approval_status(self):
        approval_status = {'approved': False,
                           'report': None}

        try:
            change_owners_map = self.get_change_owners_map()
        except OwnerNotFoundError:
            # When a change has no candidate owner, we can't
            # auto-approve the MR
            return approval_status

        report = {}
        lgtms = self.get_lgtms()

        for change_path, change_owners in change_owners_map.items():
            change_approved = False
            for owner in change_owners['owners']:
                if owner in lgtms:
                    change_approved = True
            # Each change that was not yet approved will generate
            # a report message
            if not change_approved:
                report[change_path] = (f'one of '
                                       f'{change_owners["closest_owners"]} '
                                       f'needs to approve the change')

        # Empty report means that all changes are approved
        if not report:
            approval_status['approved'] = True
            return approval_status

        # Since we have a report, let's check if that report was already
        # used for a comment
        comments = self.gitlab.get_merge_request_comments(self.mr.iid)
        for comment in comments:
            # Only interested on our own comments
            if comment['username'] != self.gitlab.user.username:
                continue

            # Only interested in comments created after the top commit
            # creation time
            comment_created_at = dateparser.parse(comment['created_at'])
            if comment_created_at < self.top_commit_created_at:
                continue

            # Removing the pre-formatted markdown from the comment
            json_body = comment['body'].lstrip('```\n').rstrip('\n```')

            try:
                body = json.loads(json_body)
                # If we find a comment equals to the report,
                # we don't return the report
                if body == report:
                    return approval_status
            except json.decoder.JSONDecodeError:
                continue

        # At this point, the MR was not approved and the report
        # will be used for creating a comment in the MR.
        json_report = json.dumps(report, indent=4)
        markdown_json_report = f'```\n{json_report}\n```'
        approval_status['report'] = markdown_json_report
        return approval_status

    def has_approval_label(self):
        labels = self.gitlab.get_merge_request_labels(self.mr.iid)
        return APPROVAL_LABEL in labels


def run(dry_run=False):
    instance = queries.get_gitlab_instance()
    settings = queries.get_app_interface_settings()
    repos = queries.get_repos_gitlab_owner(server=instance['url'])

    for repo in repos:

        gitlab_cli = GitLabApi(instance, project_url=repo,
                               settings=settings)

        project_owners = ProjectOwners(gitlab_client=gitlab_cli)

        for mr in gitlab_cli.get_merge_requests(state=MRState.OPENED):
            mr_approval = MRApproval(gitlab_client=gitlab_cli,
                                     merge_request=mr,
                                     owners=project_owners,
                                     dry_run=dry_run)

            approval_status = mr_approval.get_approval_status()
            if approval_status['approved']:
                if mr_approval.has_approval_label():
                    _LOG.info([f'Project:{gitlab_cli.project.id} '
                               f'Merge Request:{mr.iid} '
                               f'- already approved'])
                    continue
                _LOG.info([f'Project:{gitlab_cli.project.id} '
                           f'Merge Request:{mr.iid} '
                           f'- approving now'])
                if not dry_run:
                    gitlab_cli.add_label_to_merge_request(mr.iid,
                                                          APPROVAL_LABEL)
                continue

            if not dry_run:
                if mr_approval.has_approval_label():
                    _LOG.info([f'Project:{gitlab_cli.project.id} '
                               f'Merge Request:{mr.iid} '
                               f'- removing approval'])
                    gitlab_cli.remove_label_from_merge_request(mr.iid,
                                                               APPROVAL_LABEL)

            if approval_status['report'] is not None:
                _LOG.info([f'Project:{gitlab_cli.project.id} '
                           f'Merge Request:{mr.iid} '
                           f'- publishing approval report'])
                if not dry_run:
                    gitlab_cli.remove_label_from_merge_request(mr.iid,
                                                               APPROVAL_LABEL)
                    mr.notes.create({'body': approval_status['report']})
                continue

            _LOG.info([f'Project:{gitlab_cli.project.id} '
                       f'Merge Request:{mr.iid} '
                       f'- not fully approved'])