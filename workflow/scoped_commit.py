"""用临时索引提交已审查片段，工作树不改写，其他暂存内容保持。"""

from collections import defaultdict
from hashlib import sha256
from json import loads
from pathlib import Path
from tempfile import TemporaryDirectory

from workflow.gitops import ensure_baseline, git, reject_hooks
from workflow.settings import WorkflowError
from workflow.shared_changes import edits, fragment_manifest, head_file, lines, own_fragments, read_blob, review_reads_valid, save_record


def apply_edits(original, changes):
    result, cursor = [], 0
    for start, end, replacement in sorted(changes, key=lambda item: (item[0], item[1])):
        if start < cursor:
            raise WorkflowError("提交片段交叠，未修改暂存区或工作目录。")
        result.extend(original[cursor:start])
        result.extend(replacement)
        cursor = end
    return "".join([*result, *original[cursor:]]).encode("utf-8")


def merge_index(base, committed, staged):
    if staged == base or staged == committed:
        return committed
    if committed == base:
        return staged
    if None in (committed, staged):
        raise WorkflowError("删除与现有暂存修改冲突，请先处理暂存区。")
    if any(data is not None and b"\0" in data for data in (base, committed, staged)):
        raise WorkflowError("二进制改动与原暂存内容冲突，不能自动合并。")
    original, ours, theirs = lines(base), lines(committed), lines(staged)
    changes = []
    for target in (ours, theirs):
        for a, b, c, d in edits(original, target, split_insertions=False):
            change = (a, b, target[c:d])
            if change not in changes:
                changes.append(change)
    for index, (a, b, replacement) in enumerate(changes):
        for c, d, other in changes[index + 1:]:
            if (max(a, c) < min(b, d) or a == b == c == d
                    or (a == b and c < a < d) or (c == d and a < c < b)):
                raise WorkflowError("本轮提交与原暂存片段重叠，请先明确处理归属。")
    return apply_edits(original, changes)


def index_entry(repo, relative):
    result = git(repo["path"], "--literal-pathspecs", "ls-files", "--stage", "-z", "--", relative)
    if not result:
        return None, "100644"
    fields = result.split("\t", 1)[0].split()
    if fields[2] != "0":
        raise WorkflowError("暂存区存在未解决冲突，不能提交。")
    return git(repo["path"], "cat-file", "blob", fields[1], binary=True), fields[0]


def update_index(repo, environment, relative, content, mode):
    if content is None:
        git(repo["path"], "--literal-pathspecs", "update-index", "--force-remove", "--", relative,
            environment_override=environment)
        return
    oid = git(repo["path"], "hash-object", "-w", "--path=" + relative, "--stdin", input_data=content)
    git(repo["path"], "--literal-pathspecs", "update-index", "--add", "--cacheinfo", mode, oid, relative,
        environment_override=environment)


def finish_transaction(repo, transaction_path, transaction, index_path):
    lock_path = Path(str(index_path) + ".lock")
    head = git(repo["path"], "rev-parse", "HEAD")
    projected = transaction_path.with_suffix(".index")
    if head != transaction["commit"]:
        raise WorkflowError("提交事务的 HEAD 已变化，请人工核对；不会重复提交或回退代码。")
    current = sha256(index_path.read_bytes()).hexdigest() if index_path.exists() else "missing"
    if current == transaction["index_after"] and not lock_path.exists():
        transaction["status"] = "complete"
    elif current == transaction["index_before"] and lock_path.is_file() and projected.is_file():
        if sha256(lock_path.read_bytes()).hexdigest() != transaction["index_after"]:
            raise WorkflowError("暂存区锁文件已变化，请人工核对提交事务。")
        lock_path.replace(index_path)
        transaction["status"] = "complete"
    else:
        raise WorkflowError("提交已生成但暂存状态需要核对，不会覆盖现有暂存内容。")
    save_record(transaction_path, transaction)
    repo["checkout_head"] = transaction["commit"]
    repo.pop("commit_scope", None)
    return transaction["commit"]


def commit_fragments(repo, message, expected_manifest):
    ensure_baseline(repo)
    reject_hooks(repo["path"])
    index_path = Path(git(repo["path"], "rev-parse", "--path-format=absolute", "--git-path", "index"))
    transaction_path = Path(repo["change_record"]).with_suffix(".commit.json")
    if transaction_path.exists():
        previous = loads(transaction_path.read_text(encoding="utf-8"))
        if previous["status"] != "complete" and git(repo["path"], "rev-parse", "HEAD") == previous["commit"]:
            return finish_transaction(repo, transaction_path, previous, index_path)
        if (previous["status"] == "prepared" and not Path(str(index_path) + ".lock").exists()
                and git(repo["path"], "merge-base", previous["commit"], "HEAD", optional=True) != previous["commit"]):
            previous["status"] = "aborted"
            save_record(transaction_path, previous)
        if previous["status"] not in {"complete", "aborted"}:
            raise WorkflowError("上次提交未完成，请先核对记录；不会自动重复提交。")
    if "commit_scope" not in repo:
        raise WorkflowError("缺少逐片段提交范围，请重新 Review。")
    if fragment_manifest(repo) != expected_manifest:
        raise WorkflowError("最后一次 Review 后本轮差异发生变化，不能提交。")
    review_reads_valid(repo)
    grouped = defaultdict(list)
    for fragment in own_fragments(repo):
        grouped[fragment["path"]].append(fragment)
    if not grouped:
        return None
    if git(repo["path"], "ls-files", "--unmerged", "-z", binary=True):
        raise WorkflowError("暂存区存在未解决冲突，不能提交。")
    head = git(repo["path"], "rev-parse", "HEAD")
    original_index = index_path.read_bytes() if index_path.exists() else None
    with TemporaryDirectory(prefix="workflow-index-") as directory:
        commit_index, reconciled_index = Path(directory) / "commit", Path(directory) / "reconciled"
        selected_env = {"GIT_INDEX_FILE": str(commit_index)}
        staged_env = {"GIT_INDEX_FILE": str(reconciled_index)}
        git(repo["path"], "read-tree", head, environment_override=selected_env)
        if original_index is None:
            git(repo["path"], "read-tree", head, environment_override=staged_env)
        else:
            reconciled_index.write_bytes(original_index)
        for relative, fragments in grouped.items():
            fragments.sort(key=lambda fragment: (fragment["old_start"], fragment["old_end"], fragment["new_start"]))
            base, mode = head_file(repo, relative)
            if fragments[0]["kind"] == "二进制":
                selected = read_blob(repo, fragments[0]["binary_blob"])
            else:
                selected = None if fragments[0]["kind"] == "删除" else apply_edits(lines(base), [
                    (fragment["old_start"], fragment["old_end"], fragment["new"].splitlines(True)) for fragment in fragments])
            staged, staged_mode = index_entry(repo, relative)
            reconciled = merge_index(base, selected, staged)
            update_index(repo, selected_env, relative, selected, mode)
            update_index(repo, staged_env, relative, reconciled, staged_mode if staged is not None else mode)
        changed = git(repo["path"], "diff", "--cached", "--no-renames", "--name-only", "-z", head,
                      binary=True, environment_override=selected_env)
        if {part.decode("utf-8") for part in changed.split(b"\0") if part} != set(grouped):
            raise WorkflowError("临时索引与审查范围不一致，未提交。")
        tree = git(repo["path"], "write-tree", environment_override=selected_env)
        # 保持对象、引用和索引三个步骤可核对，不临时改写共享工作树。
        commit_id = git(repo["path"], "commit-tree", tree, "-p", head, input_data=message.encode("utf-8"))
        projected_path = transaction_path.with_suffix(".index")
        projected_path.write_bytes(reconciled_index.read_bytes())
        transaction = {"status": "prepared", "parent": head, "tree": tree, "commit": commit_id,
                       "manifest": expected_manifest, "index_before": sha256(original_index).hexdigest() if original_index is not None else "missing",
                       "index_after": sha256(projected_path.read_bytes()).hexdigest()}
        lock_path = Path(str(index_path) + ".lock")
        try:
            with lock_path.open("xb") as stream:
                stream.write(projected_path.read_bytes())
        except FileExistsError as error:
            raise WorkflowError("Git 暂存区正在被其他程序使用，未提交。") from error
        advanced = False
        try:
            if ((index_path.read_bytes() if index_path.exists() else None) != original_index
                    or git(repo["path"], "rev-parse", "HEAD") != head
                    or fragment_manifest(repo) != expected_manifest):
                raise WorkflowError("提交准备期间代码或暂存区变化，未提交。")
            review_reads_valid(repo)
            save_record(transaction_path, transaction)
            git(repo["path"], "update-ref", "-m", message.splitlines()[0], "refs/heads/" + repo["branch"], commit_id, head)
            advanced = True
            transaction["status"] = "committed"
            save_record(transaction_path, transaction)
            return finish_transaction(repo, transaction_path, transaction, index_path)
        finally:
            if not advanced and lock_path.exists():
                lock_path.unlink()
