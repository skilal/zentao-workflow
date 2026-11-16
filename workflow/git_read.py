"""会话仓库内的只读 Git 查询，不切换分支或获取远程对象。"""

from pathlib import PurePosixPath, PureWindowsPath

from workflow.gitops import git, repository_lock
from workflow.settings import WorkflowError


GIT_READ_TOOLS = {"git_log", "git_file", "git_files", "git_diff"}


class GitReader:
    def __init__(self, repository, skip):
        self.repository = repository
        self.skip = skip

    def git(self, *arguments):
        return git(self.repository["path"], "--literal-pathspecs", *arguments, binary=True,
                   environment_override={"GIT_NO_LAZY_FETCH": "1"})

    def path(self, value):
        if not isinstance(value, str) or not value or "\x00" in value or ":" in value:
            raise WorkflowError("Git 查询必须使用仓库内的相对路径。")
        value = value.replace("\\", "/")
        parts = PurePosixPath(value).parts
        if (PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive
                or ".." in parts or not parts or any(part.casefold() in self.skip for part in parts)):
            raise WorkflowError("Git 查询路径不能越界或访问依赖、Git 和输出目录。")
        return PurePosixPath(value).as_posix()

    def commit(self, ref):
        if not isinstance(ref, str) or not ref.strip() or ref.startswith("-") or "\x00" in ref:
            raise WorkflowError("ref 必须是明确的本地分支、远程跟踪分支、标签或提交。")
        return self.git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode("ascii").strip()

    def pagination(self, arguments, maximum, default):
        offset, limit = arguments.get("offset", 0), arguments.get("limit", default)
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= maximum:
            raise WorkflowError(f"offset 必须为非负整数，limit 必须为 1 至 {maximum}。")
        return offset, limit

    def lines(self, data, arguments):
        start, count = arguments.get("start_line", 1), arguments.get("line_count", 200)
        if type(start) is not int or type(count) is not int or start < 1 or not 1 <= count <= 500:
            raise WorkflowError("start_line 必须大于等于 1，line_count 必须为 1 至 500。")
        if len(data) > 4 * 1024 * 1024:
            raise WorkflowError("Git 内容超过 4 MiB，请缩小查询范围。")
        source = data.decode("utf-8")
        if "\x00" in source:
            raise WorkflowError("Git 内容不是可读取的 UTF-8 文本。")
        lines = source.lstrip("\ufeff").splitlines()
        end = min(len(lines), start - 1 + count)
        return {"total_lines": len(lines), "start_line": start,
                "next_line": end + 1 if end < len(lines) else None,
                "text": "\n".join(f"{index + 1}: {lines[index]}" for index in range(start - 1, end))}

    def call(self, name, arguments):
        with repository_lock(self.repository["path"]):
            if name == "git_diff":
                base = self.commit(arguments.get("base_ref"))
                commit = self.commit(arguments.get("ref"))
                path = self.path(arguments["path"]) if "path" in arguments else None
                paths = [path] if path else []
                names = self.git("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z",
                                 base, commit, "--", *paths).decode("utf-8").split("\0")
                paths = [value for value in names if value and not any(
                    part.casefold() in self.skip for part in PurePosixPath(value).parts)]
                if len(paths) > 2000:
                    raise WorkflowError("差异涉及文件超过 2000 个，请指定 path 缩小范围。")
                data = self.git("-c", "core.quotePath=false", "diff", "--no-ext-diff", "--no-textconv",
                                "--no-color", "--no-renames", "--submodule=short", base, commit, "--", *paths) if paths else b""
                return {"repository": self.repository["name"], "base_commit": base, "commit": commit,
                        "files": paths, **self.lines(data, arguments)}

            ref = arguments.get("ref", "HEAD")
            commit = self.commit(ref)
            result = {"repository": self.repository["name"], "ref": ref, "commit": commit}
            if name == "git_log":
                offset, limit = self.pagination(arguments, 100, 20)
                paths = [self.path(arguments["path"])] if "path" in arguments else []
                data = self.git("log", "--no-ext-diff", "--no-textconv", "-z",
                                "--format=%H%x00%P%x00%an%x00%aI%x00%B", f"--skip={offset}",
                                f"--max-count={limit + 1}", commit, "--", *paths)
                fields = data.decode("utf-8").split("\0")
                commits = []
                for index in range(0, len(fields) - 1, 5):
                    sha, parents, author, date, message = fields[index:index + 5]
                    commits.append({"commit": sha, "parents": parents.split(), "author": author,
                                    "date": date, "message": message.rstrip("\n")})
                return {**result, "commits": commits[:limit],
                        "next_offset": offset + limit if len(commits) > limit else None}

            if name == "git_files":
                offset, limit = self.pagination(arguments, 2000, 200)
                paths = [self.path(arguments["path"])] if "path" in arguments else []
                entries = self.git("ls-tree", "-r", "-z", "--full-tree", commit, "--", *paths)
                files = []
                for entry in entries.split(b"\0"):
                    if not entry:
                        continue
                    metadata, value = entry.split(b"\t", 1)
                    path = value.decode("utf-8")
                    if any(part.casefold() in self.skip for part in PurePosixPath(path).parts):
                        continue
                    mode, kind, sha = metadata.decode("ascii").split()
                    files.append({"path": path, "mode": mode, "type": kind, "object": sha})
                return {**result, "files": files[offset:offset + limit], "total_files": len(files),
                        "next_offset": offset + limit if offset + limit < len(files) else None}

            path = self.path(arguments.get("path"))
            entry = self.git("ls-tree", "-z", commit, "--", path).split(b"\0")[0]
            if not entry:
                raise WorkflowError("指定提交中不存在该文件。")
            metadata, value = entry.split(b"\t", 1)
            mode, kind, sha = metadata.decode("ascii").split()
            if value.decode("utf-8") != path or kind != "blob" or mode not in ("100644", "100755"):
                raise WorkflowError("指定路径不是普通 Git 文件。")
            if int(self.git("cat-file", "-s", sha)) > 4 * 1024 * 1024:
                raise WorkflowError("Git 文件超过 4 MiB，请缩小查询范围。")
            return {**result, "path": path, "source": "git", **self.lines(self.git("cat-file", "blob", sha), arguments)}
