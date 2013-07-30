from __future__ import absolute_import
import logging
import os
from celery import task, chord
from celery.utils.log import get_task_logger

from .bugzilla import bz
from .branch import Branch
from .request import AutolandRequest
from .patch import PatchSet
from .mercurial import import_and_push
from .config import config

log = get_task_logger(__name__)


# TODO: run this jask from celery cron
def autoland_get_waiting():
    """ Query Bugzilla WebService API for Autoland flagged bugs """
    for bug in bz.get_wating_auoland_bugs():
        bug_id = bug['bug_id']
        status_when = bug["status_when"]
        if AutolandRequest.processed(bug_id=bug_id, status_when=status_when):
            logging.error("Already processed bug %s requested %s", bug_id,
                          status_when)
            continue
        req = AutolandRequest(
            bug_id=bug_id,
            branches=Branch.parse_branches(bug.get('branches')),
            patches=AutolandRequest.get_waiting_patch_ids(bug),
            status_when=bug["status_when"],
            status="preprocessed",
            try_syntax=bug.get('try_syntax'))
        # save the request to get its ID from the DB
        req.save()
        process_autoland_request.delay(bug, req.id)


@task
def process_autoland_request(bug, req_id):
    bug_id = bug['bug_id']
    bug_branches = Branch.parse_branches(bug.get('branches'))

    req = AutolandRequest.get_by_id(req_id)
    res, msg = req.verify(bug)
    if not res:
        bz.remove_from_autoland_queue(req.get_patches())
        log.error("Request cannot by verified: %s", msg)
        req.update_status("not verified").save()
        ack_failed.delay(bug_id, msg)
        return

    branch_tasks = []
    patch_ids = AutolandRequest.get_waiting_patch_ids(bug)
    for branch in bug_branches:
        # TODO: use serialized Patchset instead
        patchset = dict(
            bug_id=bug_id,
            patches=patch_ids,
            branch=branch,
            status_when=bug["status_when"],
            try_syntax=bug.get('try_syntax'),
        )
        ps = PatchSet.fromdict(patchset)
        ps.save()
        branch_tasks.append(apply_patchset.si(patchset, req_id, bug))
    # run the tasks in parallel, notify on completion of all
    task = chord(branch_tasks)(ack_all_patches_pushed.s(req_id=req.id))
    req.task_id = task.task_id
    req.save()
    msg = "Autoland request for bug %s has been queued for submission" % bug_id
    ack_success.delay(bug_id, msg)
    # TODO: check for exceptions in subtasks


@task
def ack_failed(bug_id, msg):
    bz.post_failure(bug_id=bug_id, comment=msg)
    log.error("acked to bug %s: %s", bug_id, msg)


@task
def ack_success(bug_id, msg):
    bz.post_comment(bug_id=bug_id, comment=msg)
    log.info("acked to bug %s: %s", bug_id, msg)


@task
def apply_patchset(patchset, req_id, bug, attempt=1):
    """ Apply branch patches """
    bug_id = patchset["bug_id"]
    if PatchSet.processed(bug_id=bug_id, branch=patchset["branch"],
                          status_when=patchset["status_when"]):
        log.error("Already processed")
        return None, patchset

    req = AutolandRequest.get_by_id(req_id)
    res, msg = req.verify(bug)

    if not res:
        bz.remove_from_autoland_queue(req.get_patches())
        log.error("Request cannot by verified: %s", msg)
        req.update_status("not verified").save()
        ack_failed.delay(bug_id, msg)
        return None, patchset

    branch = Branch.get_by_name(patchset["branch"])
    if branch.tree_closed():
        retry_in = config["hg_tree_closure_retry_interval"]
        attempts = config["tree_closure_max_attempts"]

        if attempt >= attempts:
            msg = "Branch %s is closed. Won't retry anymore" % branch.name
            log.error(msg)
            ack_failed.delay(bug_id, msg)
            return None, patchset

        log.warning("Branch %s is closed. Will retry in %s secs",
                    branch.name, retry_in)
        kwargs = dict(patchset=patchset, req_id=req_id, bug=bug,
                      attempt=attempt + 1)
        apply_patchset.retry(kwargs=kwargs, countdown=retry_in,
                             max_retries=attempts)

    ps = PatchSet.fromdict(patchset)
    ps.update_status("in progress").save()
    bz.update_autoland_status("running", req.get_patches())

    base_work_dir = config["hg_base_work_dir"]
    repo = os.path.join(base_work_dir, branch.name)
    patches_dir = os.path.join(base_work_dir, "%s.patches" % branch.name)

    rev = import_and_push(
        repo=repo, bug=bug, branch_name=branch.name, patch_ids=ps.patches,
        patches_dir=patches_dir, add_try_commit=branch.add_try_commit,
        try_syntax=ps.try_syntax, pull_repo=branch.pull_repo,
        push_repo=branch.push_repo, ssh_username=config["hg_ssh_username"],
        ssh_key=config["hg_ssh_key"])
    ps.changeset = rev
    ps.status = "pushed"
    ps.save()
    # TODO: print hg and tbpl urls
    msg = "Bug %s patches have been pushed to %s" % (bug_id, branch.name)
    ack_success.delay(bug_id, msg)
    return True, patchset


@task
def ack_all_patches_pushed(results, req_id):
    """ Submit and everall bz post that all patches are applied """
    req = AutolandRequest.get_by_id(req_id)
    if all(res for res, _ in results):
        req.update_status("success").save()
        log.error("yay, all applied")
        bz.update_autoland_status("success", req.get_patches())
        msg = "Bug %s patches have been pushed to %s" % (req.bug_id,
                                                         req.branches)
        ack_success.delay(req.bug_id, msg)
    else:
        failed = [p for res, p in results if not res]
        log.error("some failed: %s", failed)
        req.update_status("failure").save()
        bz.update_autoland_status("failed", req.get_patches())

    branches = [p["branch"] for _, p in results]
    log.info("branches: %s", branches)
