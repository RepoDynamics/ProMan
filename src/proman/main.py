"""Main event handler."""


from pathlib import Path
import json
from typing import Literal
import re
import datetime

from loggerman import logger
from markitup import html, md
import pylinks
import pyserials as _ps

from github_contexts import GitHubContext
import conventional_commits
import pkgdata
import controlman
from controlman.datatype import (
    InitCheckAction, Branch, Emoji,
    Commit, NonConventionalCommit, Label, PrimaryActionCommitType
)
import gittidy
import versionman

from proman.datatype import TemplateType, FileChangeType, RepoFileType, BranchType
from proman.output_writer import OutputWriter
from proman.repo_config import RepoConfig
from proman import hook_runner
from proman.data_manager import DataManager
from proman import change_detector


class EventHandler:

    _REPODYNAMICS_BOT_USER = ("RepoDynamicsBot", "146771514+RepoDynamicsBot@users.noreply.github.com")

    _MARKER_COMMIT_START = "<!-- Begin primary commit summary -->"
    _MARKER_COMMIT_END = "<!-- End primary commit summary -->"
    _MARKER_TASKLIST_START = "<!-- Begin secondary commits tasklist -->"
    _MARKER_TASKLIST_END = "<!-- End secondary commits tasklist -->"
    _MARKER_REFERENCES_START = "<!-- Begin references -->"
    _MARKER_REFERENCES_END = "<!-- End references -->"
    _MARKER_TIMELINE_START = "<!-- Begin timeline -->"
    _MARKER_TIMELINE_END = "<!-- End timeline -->"
    _MARKER_ISSUE_NR_START = "<!-- Begin issue number -->"
    _MARKER_ISSUE_NR_END = "<!-- End issue number -->"

    def __init__(
        self,
        template_type: TemplateType,
        context_manager: GitHubContext,
        admin_token: str,
        path_repo_base: str,
        path_repo_head: str,
    ):
        self._template_type = template_type
        self._context = context_manager
        self._path_repo_base = Path(path_repo_base)
        self._path_repo_head = Path(path_repo_head)
        self._output = OutputWriter(context=self._context)

        self._data_main = DataManager(controlman.from_json_file(repo_path=self._path_repo_base))

        repo_user = self._context.repository_owner
        repo_name = self._context.repository_name
        self._gh_api_admin = pylinks.api.github(token=admin_token).user(repo_user).repo(repo_name)
        self._gh_api = pylinks.api.github(token=self._context.token).user(repo_user).repo(repo_name)
        self._gh_link = pylinks.site.github.user(repo_user).repo(repo_name)
        self._has_admin_token = bool(admin_token)
        self._repo_config = RepoConfig(
            gh_api=self._gh_api_admin if self._has_admin_token else self._gh_api,
            default_branch_name=self._context.event.repository.default_branch
        )

        git_user = (self._context.event.sender.login, self._context.event.sender.github_email)
        # TODO: Check again when gittidy is finalized; add section titles
        self._git_base = gittidy.Git(
            path=self._path_repo_base,
            user=git_user,
            user_scope="global",
            committer=self._REPODYNAMICS_BOT_USER,
            committer_scope="local",
            committer_persistent=True,
        )
        self._git_head = gittidy.Git(
            path=self._path_repo_head,
            user=git_user,
            user_scope="global",
            committer=self._REPODYNAMICS_BOT_USER,
            committer_scope="local",
            committer_persistent=True,
        )

        proman_version = pkgdata.get_version_from_caller()
        self._template_name_ver = f"{self._template_type.value} v{proman_version}"
        self._is_pypackit = self._template_type is TemplateType.PYPACKIT

        self._failed = False
        self._branch_name_memory_autoupdate: str | None = None
        self._summary_oneliners: list[str] = []
        self._summary_sections: list[str | html.ElementCollection | html.Element] = []
        self._summary_event_description: str = ""
        return

    def run(self) -> tuple[dict, str]:
        self._run_event()
        output = self._output.generate(failed=self._failed)
        summary = self.assemble_summary()
        return output, summary

    def _run_event(self) -> None:
        ...

    def run_sync_fix(
        self,
        action: InitCheckAction,
        branch: Branch | None = None,
        testpypi_publishable: bool = False,
        version: str | None = None
    ) -> tuple[dict[str, bool], controlman.ControlCenterContentManager, str]:

        def decide_jobs():
            package_setup_files_changed = any(
                filepath in changed_file_groups[RepoFileType.DYNAMIC]
                for filepath in (
                    controlman.path.FILE_PYTHON_PYPROJECT,
                    controlman.path.FILE_PYTHON_MANIFEST,
                )
            )
            package_files_changed = bool(
                changed_file_groups[RepoFileType.PACKAGE]
            ) or package_setup_files_changed
            out = {
                "website_build": (
                    bool(changed_file_groups[RepoFileType.WEBSITE])
                    or bool(changed_file_groups[RepoFileType.PACKAGE])
                ),
                "package_test": bool(changed_file_groups[RepoFileType.TEST]) or package_files_changed,
                "package_build": bool(
                    changed_file_groups[RepoFileType.PACKAGE]
                ) or package_setup_files_changed,
                "package_lint": bool(changed_file_groups[RepoFileType.PACKAGE]) or package_setup_files_changed,
                "package_publish_testpypi": package_files_changed and testpypi_publishable,
            }
            return out

        if (version or action is InitCheckAction.PULL) and not branch:
            raise RuntimeError("branch must be provided when action is 'pull' or version is provided.")
        # branch = branch or self.resolve_branch(self._context.head_ref)
        cc_manager = self.get_cc_manager(future_versions={branch.name: version} if version else None)
        changed_file_groups = self._action_file_change_detector(control_center_manager=cc_manager)
        hash_hooks = self._action_hooks(
            action=action,
            branch=branch,
            base=False,
            ref_range=(self._context.hash_before, self._context.hash_after),
        ) if self._data_main["workflow"].get("pre_commit") else None
        for file_type in (RepoFileType.SUPERMETA, RepoFileType.META, RepoFileType.DYNAMIC):
            if changed_file_groups[file_type]:
                hash_meta = self._action_meta(action=action, cc_manager=cc_manager, base=False, branch=branch)
                ccm_branch = cc_manager.generate_data()
                break
        else:
            hash_meta = None
            ccm_branch = controlman.read_from_json_file(path_repo=self._path_repo_head)
        latest_hash = self._git_head.push() if hash_hooks or hash_meta else self._context.hash_after
        job_runs = decide_jobs()
        return job_runs, ccm_branch, latest_hash

    @logger.sectioner("File Change Detector")
    def _action_file_change_detector(self, data: DataManager) -> tuple[RepoFileType, ...]:
        changes = self._git_head.changed_files(
            ref_start=self._context.hash_before, ref_end=self._context.hash_after
        )
        logger.debug("Detected changed files", json.dumps(changes, indent=3))
        full_info = change_detector.detect(data=data, changes=changes)
        changed_filetypes = {}
        headers = "".join(
            [f"<th>{header}</th>" for header in ("Type", "Subtype", "Change", "Dynamic", "Path")])
        rows = [f"<tr>{headers}</tr>"]
        for typ, subtype, change_type, is_dynamic, path in sorted(full_info, key=lambda x: (x[0].value, x[1])):
            changed_filetypes.setdefault(typ, []).append(change_type)
            if is_dynamic:
                changed_filetypes.setdefault(RepoFileType.DYNAMIC, []).append(change_type)
            dynamic = f'<td title="{'Dynamic' if is_dynamic else 'Static'}">{'✅' if is_dynamic else '❌'}</td>'
            change_sig = change_type.value
            change = f'<td title="{change_sig.title}">{change_sig.emoji}</td>'
            subtype = subtype or Path(path).stem
            rows.append(
                f"<tr><td>{typ.value}</td><td>{subtype}</td>{change}{dynamic}<td><code>{path}</code></td></tr>"
            )
        if not changed_filetypes:
            oneliner = "This event did not change any files."
            section = None
        else:
            section_intro = []
            oneliner_list = []
            oneliner_table_rows = ["<tr><th>Type</th><th>Changes</th></tr>"]
            has_broken_changes = False
            if RepoFileType.DYNAMIC in changed_filetypes:
                warning = "⚠️ Dynamic files were changed; make sure to double-check that everything is correct."
                section_intro.append(warning)
                oneliner_list.append(warning)
            for file_type, change_list in changed_filetypes.items():
                change_list = sorted(set(change_list), key=lambda x: x.value.title)
                changes = []
                for change_type in change_list:
                    if change_type in (FileChangeType.BROKEN, FileChangeType.UNKNOWN):
                        has_broken_changes = True
                    changes.append(
                        f'<div title="{change_type.value.title}">{change_type.value.emoji}</div>'
                    )
                changes_cell = "&nbsp;".join(changes)
                oneliner_table_rows.append(
                    f"<tr><td>{file_type.value}</td><td>{changes_cell}</td></tr>"
                )
            if has_broken_changes:
                warning = "⚠️ Some changes were marked as 'broken' or 'unknown'; please investigate."
                section_intro.append(warning)
                oneliner_list.append(warning)
            oneliner_list.append("The following filetypes were changed:")
            oneliner = html.ElementCollection([html.ul(oneliner_list), html.table(oneliner_table_rows)])
            section_intro.append("The following files changed during this event:")
            legend = [f"{status.value.emoji}  {status.value.title}" for status in FileChangeType]
            color_legend = html.details(content=[html.ul(legend)], summary="Color Legend")
            section = html.ElementCollection([html.ul(section_intro), html.table(rows), color_legend])
        self.add_summary(
            name="File Change Detector",
            status="pass",
            oneliner=oneliner,
            details=section,
        )
        return tuple(changed_filetypes.keys())

    @logger.sectioner("Configuration Management")
    def _action_meta(
        self,
        action: InitCheckAction,
        cc_manager: controlman.ControlCenterManager,
        base: bool,
        branch: Branch | None = None
    ) -> str | None:
        name = "Configuration Management"
        # if not action:
        #     action = InitCheckAction(
        #         self._ccm_main["workflow"]["init"]["meta_check_action"][self._event_type.value]
        #     )
        logger.info(f"Action: {action.value}")
        if action == InitCheckAction.NONE:
            self.add_summary(
                name=name,
                status="skip",
                oneliner="Meta synchronization is disabled for this event type❗",
            )
            logger.info("Meta synchronization is disabled for this event type; skip❗")
            return
        git = self._git_base if base else self._git_head
        if action == InitCheckAction.PULL:
            pr_branch_name = self.switch_to_autoupdate_branch(typ="meta", git=git)
        meta_results, meta_changes, meta_summary = cc_manager.compare_files()
        # logger.success("Meta synchronization completed.", {"result": meta_results})
        # logger.success("Meta synchronization summary:", meta_summary)
        # logger.success("Meta synchronization changes:", meta_changes)
        meta_changes_any = any(any(change.values()) for change in meta_changes.values())
        # Push/amend/pull if changes are made and action is not 'fail' or 'report'
        commit_hash = None
        if action not in [
            InitCheckAction.FAIL, InitCheckAction.REPORT
        ] and meta_changes_any:
            cc_manager.apply_changes()
            commit_msg = conventional_commits.message.create(
                typ=self._data_main["commit"]["secondary_action"]["auto-update"]["type"],
                description="Sync dynamic files",
            )
            commit_hash_before = git.commit_hash_normal()
            commit_hash_after = git.commit(message=str(commit_msg), stage="all")
            commit_hash = self._action_hooks(
                action=InitCheckAction.AMEND,
                branch=branch,
                base=base,
                ref_range=(commit_hash_before, commit_hash_after),
                internal=True,
            ) or commit_hash_after
            if action == InitCheckAction.PULL:
                git.push(target="origin", set_upstream=True)
                pull_data = self._gh_api_admin.pull_create(
                    head=pr_branch_name,
                    base=self._branch_name_memory_autoupdate,
                    title=commit_msg.summary,
                    body=commit_msg.body,
                )
                self.switch_back_from_autoupdate_branch(git=git)
                commit_hash = None
        if not meta_changes_any:
            oneliner = "All dynamic files are in sync with meta content."
            logger.info(oneliner)
        else:
            oneliner = "Some dynamic files were out of sync with meta content."
            if action in [InitCheckAction.PULL, InitCheckAction.COMMIT, InitCheckAction.AMEND]:
                oneliner += " These were resynchronized and applied to "
                if action == InitCheckAction.PULL:
                    link = html.a(href=pull_data["url"], content=pull_data["number"])
                    oneliner += f"branch '{pr_branch_name}' and a pull request ({link}) was created."
                else:
                    link = html.a(
                        href=str(self._gh_link.commit(commit_hash)), content=commit_hash[:7]
                    )
                    oneliner += "the current branch " + (
                        f"in a new commit (hash: {link})"
                        if action == InitCheckAction.COMMIT
                        else f"by amending the latest commit (new hash: {link})"
                    )
        self.add_summary(
            name=name,
            status="fail" if meta_changes_any and action in [
               InitCheckAction.FAIL,
               InitCheckAction.REPORT,
               InitCheckAction.PULL
            ] else "pass",
            oneliner=oneliner,
            details=meta_summary,
        )
        return commit_hash

    @logger.sectioner("Workflow Hooks")
    def _action_hooks(
        self,
        action: InitCheckAction,
        base: bool,
        branch: Branch | None = None,
        ref_range: tuple[str, str] | None = None,
        internal: bool = False,
    ) -> str | None:
        name = "Workflow Hooks"
        # if not action:
        #     action = InitCheckAction(
        #         self._ccm_main["workflow"]["init"]["hooks_check_action"][self._event_type.value]
        #     )
        logger.info(f"Action: {action.value}")
        if action == InitCheckAction.NONE:
            self.add_summary(
                name=name,
                status="skip",
                oneliner="Hooks are disabled for this event type❗",
            )
            logger.info("Hooks are disabled for this event type; skip❗")
            return
        config = self._data_main["workflow"]["pre_commit"]
        if not config:
            if not internal:
                oneliner = "Hooks are enabled but no pre-commit config set in 'workflow.pre_commit'❗"
                logger.error(oneliner)
                self.add_summary(
                    name=name,
                    status="fail",
                    oneliner=oneliner,
                )
            return
        input_action = (
            action
            if action in [InitCheckAction.REPORT, InitCheckAction.AMEND, InitCheckAction.COMMIT]
            else (InitCheckAction.REPORT if action == InitCheckAction.FAIL else InitCheckAction.COMMIT)
        )
        commit_msg = (
            conventional_commits.message.create(
                typ=self._data_main["commit"]["secondary_action"]["auto-update"]["type"],
                description="Apply automatic fixes made by workflow hooks",
            )
            if action in [InitCheckAction.COMMIT, InitCheckAction.PULL]
            else ""
        )
        git = self._git_base if base else self._git_head
        if action == InitCheckAction.PULL:
            pr_branch = self.switch_to_autoupdate_branch(typ="hooks", git=git)
        hooks_output = hook_runner.run(
            git=git,
            ref_range=ref_range,
            action=input_action.value,
            commit_message=str(commit_msg),
            config=config,
        )
        passed = hooks_output["passed"]
        modified = hooks_output["modified"]
        # Push/amend/pull if changes are made and action is not 'fail' or 'report'
        if action not in [InitCheckAction.FAIL, InitCheckAction.REPORT] and modified:
            # self.push(amend=action == InitCheckAction.AMEND, set_upstream=action == InitCheckAction.PULL)
            if action == InitCheckAction.PULL:
                git.push(target="origin", set_upstream=True)
                pull_data = self._gh_api_admin.pull_create(
                    head=pr_branch,
                    base=branch.name,
                    title=commit_msg.summary,
                    body=commit_msg.body,
                )
                self.switch_back_from_autoupdate_branch(git=git)
        commit_hash = None
        if action == InitCheckAction.PULL and modified:
            link = html.a(href=pull_data["url"], content=pull_data["number"])
            target = f"branch '{pr_branch}' and a pull request ({link}) was created"
        if action in [InitCheckAction.COMMIT, InitCheckAction.AMEND] and modified:
            commit_hash = hooks_output["commit_hash"]
            link = html.a(href=str(self._gh_link.commit(commit_hash)), content=commit_hash[:7])
            target = "the current branch " + (
                f"in a new commit (hash: {link})"
                if action == InitCheckAction.COMMIT
                else f"by amending the latest commit (new hash: {link})"
            )
        if passed:
            oneliner = (
                "All hooks passed without making any modifications."
                if not modified
                else (
                    "All hooks passed in the second run. "
                    f"The modifications made during the first run were applied to {target}."
                )
            )
        elif action in [InitCheckAction.FAIL, InitCheckAction.REPORT]:
            mode = "some failures were auto-fixable" if modified else "failures were not auto-fixable"
            oneliner = f"Some hooks failed ({mode})."
        elif modified:
            oneliner = (
                "Some hooks failed even after the second run. "
                f"The modifications made during the first run were still applied to {target}."
            )
        else:
            oneliner = "Some hooks failed (failures were not auto-fixable)."
        if not internal:
            self.add_summary(
                name=name,
                status="fail" if not passed or (action == InitCheckAction.PULL and modified) else "pass",
                oneliner=oneliner,
                details=hooks_output["summary"],
            )
        return commit_hash

    def add_summary(
        self,
        name: str,
        status: Literal["pass", "fail", "skip", "warning"],
        oneliner: str,
        details: str | html.Element | html.ElementCollection | None = None,
    ):
        if status == "fail":
            self._failed = True
        self._summary_oneliners.append(f"{Emoji[status]}&nbsp;<b>{name}</b>: {oneliner}")
        if details:
            self._summary_sections.append(f"<h2>{name}</h2>\n\n{details}\n\n")
        return

    def get_cc_manager(
        self,
        base: bool = False,
        data_before: dict | None = None,
        data_main: dict | None = None,
        future_versions: dict[str, str | versionman.PEP440SemVer] | None = None,
    ) -> controlman.CenterManager:
        return controlman.manager(
            repo=self._git_base if base else self._git_head,
            data_before=data_before,
            data_main=data_main or self._data_main,
            github_token=self._context.token,
            future_versions=future_versions,
        )

    def _get_latest_version(
        self,
        branch: str | None = None,
        dev_only: bool = False,
        base: bool = True,
    ) -> tuple[versionman.PEP440SemVer | None, int | None]:

        def get_latest_version() -> versionman.PEP440SemVer | None:
            tags_lists = git.get_tags()
            if not tags_lists:
                return
            for tags_list in tags_lists:
                ver_tags = []
                for tag in tags_list:
                    if tag.startswith(ver_tag_prefix):
                        ver_tags.append(versionman.PEP440SemVer(tag.removeprefix(ver_tag_prefix)))
                if ver_tags:
                    if dev_only:
                        ver_tags = sorted(ver_tags, reverse=True)
                        for ver_tag in ver_tags:
                            if ver_tag.release_type == "dev":
                                return ver_tag
                    else:
                        return max(ver_tags)
            return

        git = self._git_base if base else self._git_head
        ver_tag_prefix = self._data_main["tag.version.prefix"]
        if branch:
            git.stash()
            curr_branch = git.current_branch_name()
            git.checkout(branch=branch)
        latest_version = get_latest_version()
        distance = git.get_distance(
            ref_start=f"refs/tags/{ver_tag_prefix}{latest_version.input}"
        ) if latest_version else None
        if branch:
            git.checkout(branch=curr_branch)
            git.stash_pop()
        if not latest_version and not dev_only:
            logger.error(f"No matching version tags found with prefix '{ver_tag_prefix}'.")
        return latest_version, distance

    def _get_commits(self, base: bool = False) -> list[Commit]:
        git = self._git_base if base else self._git_head
        commits = git.get_commits(f"{self._context.hash_before}..{self._context.hash_after}")
        logger.info("Read commits from git history", json.dumps(commits, indent=4))
        parser = conventional_commits.parser.create(
            types=self._data_main.get_all_conventional_commit_types(secondary_custom_only=False),
        )
        parsed_commits = []
        for commit in commits:
            conv_msg = parser.parse(message=commit["msg"])
            if not conv_msg:
                parsed_commits.append(
                    Commit(
                        **commit, group_data=NonConventionalCommit()
                    )
                )
            else:
                group = self._data_main.get_commit_type_from_conventional_type(conv_type=conv_msg.type)
                commit["msg"] = conv_msg
                parsed_commits.append(Commit(**commit, group_data=group))
        return parsed_commits

    def _tag_version(self, ver: str | versionman.PEP440SemVer, base: bool, msg: str = "") -> str:
        tag_prefix = self._data_main["tag.version.prefix"]
        tag = f"{tag_prefix}{ver}"
        if not msg:
            msg = f"Release version {ver}"
        git = self._git_base if base else self._git_head
        git.create_tag(tag=tag, message=msg)
        return tag

    def _update_issue_status_labels(
        self, issue_nr: int, labels: list[Label], current_label: Label
    ) -> None:
        for label in labels:
            if label.name != current_label.name:
                self._gh_api.issue_labels_remove(number=issue_nr, label=label.name)
        return

    def resolve_branch(self, branch_name: str | None = None) -> Branch:
        if not branch_name:
            branch_name = self._context.ref_name
        if branch_name == self._context.event.repository.default_branch:
            return Branch(type=BranchType.MAIN, name=branch_name)
        for branch_type, branch_data in self._data_main["branch"].items():
            if branch_name.startswith(branch_data["name"]):
                branch_type = BranchType(branch_type)
                suffix_raw = branch_name.removeprefix(branch_data["name"])
                if branch_type is BranchType.RELEASE:
                    suffix = int(suffix_raw)
                elif branch_type is BranchType.PRE:
                    suffix = versionman.PEP440SemVer(suffix_raw)
                elif branch_type is BranchType.DEV:
                    issue_num, target_branch = suffix_raw.split("/", 1)
                    suffix = (int(issue_num), target_branch)
                else:
                    suffix = suffix_raw
                return Branch(type=branch_type, name=branch_name, prefix=branch_data["name"], suffix=suffix)
        return Branch(type=BranchType.OTHER, name=branch_name)

    def _extract_tasklist(self, body: str) -> list[dict[str, bool | str | list]]:
        """
        Extract the implementation tasklist from the pull request body.

        Returns
        -------
        A list of dictionaries, each representing a tasklist entry.
        Each dictionary has the following keys:
        - complete : bool
            Whether the task is complete.
        - summary : str
            The summary of the task.
        - description : str
            The description of the task.
        - sublist : list[dict[str, bool | str | list]]
            A list of dictionaries, each representing a subtask entry, if any.
            Each dictionary has the same keys as the parent dictionary.
        """

        def extract(tasklist_string: str, level: int = 0) -> list[dict[str, bool | str | list]]:
            # Regular expression pattern to match each task item
            task_pattern = rf'{" " * level * 2}- \[(X| )\] (.+?)(?=\n{" " * level * 2}- \[|\Z)'
            # Find all matches
            matches = re.findall(task_pattern, tasklist_string, flags=re.DOTALL)
            # Process each match into the required dictionary format
            tasklist_entries = []
            for match in matches:
                complete, summary_and_desc = match
                summary_and_desc_split = summary_and_desc.split('\n', 1)
                summary = summary_and_desc_split[0]
                description = summary_and_desc_split[1] if len(summary_and_desc_split) > 1 else ''
                if description:
                    sublist_pattern = r'^( *- \[(?:X| )\])'
                    parts = re.split(sublist_pattern, description, maxsplit=1, flags=re.MULTILINE)
                    description = parts[0]
                    if len(parts) > 1:
                        sublist_str = ''.join(parts[1:])
                        sublist = extract(sublist_str, level + 1)
                    else:
                        sublist = []
                else:
                    sublist = []
                tasklist_entries.append({
                    'complete': complete == 'X',
                    'summary': summary.strip(),
                    'description': description.rstrip(),
                    'sublist': sublist
                })
            return tasklist_entries

        pattern = rf"{self._MARKER_TASKLIST_START}(.*?){self._MARKER_TASKLIST_END}"
        match = re.search(pattern, body, flags=re.DOTALL)
        return extract(match.group(1).strip()) if match else []

    def _add_to_timeline(
        self,
        entry: str,
        body: str,
        issue_nr: int | None = None,
        comment_id: int | None = None,
    ):
        now = datetime.datetime.now(tz=datetime.UTC).strftime("%Y.%m.%d %H:%M:%S")
        timeline_entry = (
            f"- **{now}**: {entry}"
        )
        pattern = rf"({self._MARKER_TIMELINE_START})(.*?)({self._MARKER_TIMELINE_END})"
        replacement = r"\1\2" + timeline_entry + "\n" + r"\3"
        new_body = re.sub(pattern, replacement, body, flags=re.DOTALL)
        if issue_nr:
            self._gh_api.issue_update(number=issue_nr, body=new_body)
        elif comment_id:
            self._gh_api.issue_comment_update(comment_id=comment_id, body=new_body)
        else:
            logger.error(
                "Failed to add to timeline", "Neither issue nor comment ID was provided."
            )
        return new_body

    def switch_to_autoupdate_branch(self, typ: Literal["hooks", "meta"], git: gittidy.Git) -> str:
        current_branch = git.current_branch_name()
        new_branch_prefix = self._data_main["branch.auto.name"]
        new_branch_name = f"{new_branch_prefix}{current_branch}/{typ}"
        git.stash()
        git.checkout(branch=new_branch_name, reset=True)
        logger.info(f"Switch to CI branch '{new_branch_name}' and reset it to '{current_branch}'.")
        self._branch_name_memory_autoupdate = current_branch
        return new_branch_name

    def switch_back_from_autoupdate_branch(self, git: gittidy.Git) -> None:
        if self._branch_name_memory_autoupdate:
            git.checkout(branch=self._branch_name_memory_autoupdate)
            git.stash_pop()
            self._branch_name_memory_autoupdate = None
        return

    def assemble_summary(self) -> str:
        github_context, event_payload = (
            html.details(content=md.code_block(str(data), lang="yaml"), summary=summary)
            for data, summary in (
                (self._context, "🎬 GitHub Context"),
                (self._context.event, "📥 Event Payload"),
            )
        )
        intro = [
            f"<b>Status</b>: {Emoji.FAIL if self._failed else Emoji.PASS}",
            f"<b>Event</b>: {self._summary_event_description}",
            f"<b>Summary</b>: {html.ul(self._summary_oneliners)}",
            f"<b>Data</b>: {html.ul([github_context, event_payload])}",
        ]
        summary = html.ElementCollection([html.h(1, "Workflow Report"), html.ul(intro)])
        logs = html.ElementCollection(
            [
                html.h(2, "🪵 Logs"),
                html.details(logger.html_log, "Log"),
            ]
        )
        summaries = html.ElementCollection(self._summary_sections)
        path = Path("./proman_artifacts")
        path.mkdir(exist_ok=True)
        with open(path / "log.html", "w") as f:
            f.write(str(logs))
        with open(path / "report.html", "w") as f:
            f.write(str(summaries))
        return str(summary)

    def error_unsupported_triggering_action(self):
        event_name = self._context.event_name.value
        action_name = self._context.event.action.value
        action_err_msg = f"Unsupported triggering action for '{event_name}' event"
        action_err_details = (
            f"The workflow was triggered by an event of type '{event_name}', "
            f"but the triggering action '{action_name}' is not supported."
        )
        self.add_summary(
            name="Event Handler",
            status="fail",
            oneliner=action_err_msg,
            details=action_err_details,
        )
        logger.critical(action_err_msg, action_err_details)
        return

    def _add_reference_to_dev_protocol(self, protocol: str, reference: str) -> str:
        entry = f"- {reference}"
        pattern = rf"({self._MARKER_REFERENCES_START})(.*?)({self._MARKER_REFERENCES_END})"
        replacement = r"\1\2" + entry + "\n" + r"\3"
        return re.sub(pattern, replacement, protocol, flags=re.DOTALL)

    def _add_readthedocs_reference_to_pr(
        self,
        pull_nr: int,
        update: bool = True,
        pull_body: str = ""
    ) -> str | None:

        def create_readthedocs_preview_url():
            # Ref: https://github.com/readthedocs/actions/blob/v1/preview/scripts/edit-description.js
            # Build the ReadTheDocs website for pull-requests and add a link to the pull request's description.
            # Note: Enable "Preview Documentation from Pull Requests" in ReadtheDocs project at https://docs.readthedocs.io/en/latest/pull-requests.html
            config = self._data_main["tool.readthedocs.config.workflow"]
            domain = "org.readthedocs.build" if config["platform"] == "community" else "com.readthedocs.build"
            slug = config["name"]
            url = f"https://{slug}--{pull_nr}.{domain}/"
            if config["version_scheme"]["translation"]:
                language = config["language"]
                url += f"{language}/{pull_nr}/"
            return url

        if not self._data_main["tool.readthedocs"]:
            return
        url = create_readthedocs_preview_url()
        reference = f"[Website Preview on ReadTheDocs]({url})"
        if not pull_body:
            pull_body = self._gh_api.pull(number=pull_nr)["body"]
        new_body = self._add_reference_to_dev_protocol(protocol=pull_body, reference=reference)
        if update:
            self._gh_api.pull_update(number=pull_nr, body=new_body)
        return new_body

    def set_event_description(self, description: str) -> None:
        self._summary_event_description = description
        logger.info("Event", description)
        return

    def create_branch_name_release(self, major_version: int) -> str:
        """Generate the name of the release branch for a given major version."""
        release_branch_prefix = self._data_main["branch.release.name"]
        return f"{release_branch_prefix}{major_version}"

    def create_branch_name_prerelease(self, version: versionman.PEP440SemVer) -> str:
        """Generate the name of the pre-release branch for a given version."""
        pre_release_branch_prefix = self._data_main["branch.pre.name"]
        return f"{pre_release_branch_prefix}{version}"

    def create_branch_name_implementation(self, issue_nr: int, base_branch_name: str) -> str:
        """Generate the name of the development branch for a given issue number and base branch."""
        dev_branch_prefix = self._data_main["branch.dev.name"]
        return f"{dev_branch_prefix}{issue_nr}/{base_branch_name}"

    def read_announcement_file(self, base: bool, data: _ps.NestedDict) -> str | None:
        filepath = data["announcement.path"]
        if not filepath:
            return
        path_root = self._path_repo_base if base else self._path_repo_head
        fullpath = path_root / filepath
        return fullpath.read_text() if fullpath.is_file() else None

    def write_announcement_file(self, announcement: str, base: bool, data: _ps.NestedDict) -> None:
        announcement_data = data["announcement"]
        if not announcement_data:
            return
        if announcement:
            announcement = f"{announcement.strip()}\n"
        path_root = self._path_repo_base if base else self._path_repo_head
        with open(path_root / announcement_data["path"], "w") as f:
            f.write(announcement)
        return

    @staticmethod
    def get_next_version(
        version: versionman.PEP440SemVer,
        action: PrimaryActionCommitType
    ) -> versionman.PEP440SemVer:
        if action is PrimaryActionCommitType.RELEASE_MAJOR:
            if version.major == 0:
                return version.next_minor
            return version.next_major
        if action == PrimaryActionCommitType.RELEASE_MINOR:
            if version.major == 0:
                return version.next_patch
            return version.next_minor
        if action == PrimaryActionCommitType.RELEASE_PATCH:
            return version.next_patch
        if action == PrimaryActionCommitType.RELEASE_POST:
            return version.next_post
        return version

    @staticmethod
    def write_tasklist(entries: list[dict[str, bool | str | list]]) -> str:
        """Write an implementation tasklist as Markdown string.

        Parameters
        ----------
        entries : list[dict[str, bool | str | list]]
            A list of dictionaries, each representing a tasklist entry.
            The format of each dictionary is the same as that returned by
            `_extract_tasklist_entries`.
        """
        string = []

        def write(entry_list, level=0):
            for entry in entry_list:
                description = f"{entry['description']}\n" if entry['description'] else ''
                check = 'X' if entry['complete'] else ' '
                string.append(f"{' ' * level * 2}- [{check}] {entry['summary']}\n{description}")
                write(entry['sublist'], level + 1)

        write(entries)
        return "".join(string).rstrip()