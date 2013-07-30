import os
import re
import logging
from hgext.mq import patchheader
from util.commands import run_cmd, get_output
from .bugzilla import bz
from util.hg import mercurial, cleanOutgoingRevs, apply_and_push, get_revision
from util.retry import retry
from .config import config

log = logging.getLogger(__name__)


class mq(object):
    hg = config.get("hg_binary", "hg")

    @classmethod
    def _run_hg(cls, command, repo=None, want_output=False):
        cmd = [cls.hg, '--config', 'extensions.mq=']
        if repo:
            cmd.extend(["-R", repo])
        if want_output:
            return get_output(cmd + command)
        else:
            run_cmd(cmd + command)

    @classmethod
    def qimport(cls, repo, patch_file):
        cls._run_hg(repo=repo, command=["qimport", patch_file])

    @classmethod
    def qpush(cls, repo):
        cls._run_hg(repo=repo, command=["qpush"])

    @classmethod
    def qheader(cls, repo):
        cls._run_hg(repo=repo, command=["qheader"], want_output=True)

    @classmethod
    def qrefresh(cls, repo, user=None, message=None):
        cmd = ["qrefresh"]
        if user:
            cmd.extend(["-u", user])
        if message:
            cmd.extend(["-m", message])

        cls._run_hg(repo=repo, command=cmd)

    @classmethod
    def qnew(cls, repo, message, user, queue_name):
        cls._run_hg(repo=repo, command=["qnew", "-m", message, "-u", user,
                                        queue_name])

    @classmethod
    def qpop(cls, repo, pop_all=True):
        cmd = ["qpop"]
        if pop_all:
            cmd.append("-a")
        cls._run_hg(repo=repo, command=cmd)

    @classmethod
    def qfinish(cls, repo):
        cls._run_hg(repo=repo, command=["qfinish", "-a"])

    @classmethod
    def clean_repo(cls, repo, pull_repo):
        if os.path.exists(os.path.join(repo, '.hg')):
            log.info("Cleaning up the repo")
            mq.qpop(repo=repo)
            run_cmd(["rm", "-rf", os.path.join(repo, ".hg/patches")])
            run_cmd([cls.hg, "update", "-C"], cwd=repo)
            run_cmd([cls.hg, "--config", "extensions.purge=", "purge"],
                    cwd=repo)
            cleanOutgoingRevs(repo, pull_repo, username=None, sshKey=None)


def import_patch(repo, patch, patch_file, bug, branch, author):
    """
    Import patch file patch into a mercurial queue.

    Import is used to pull required header information, and to
    automatically perform a commit for each patch
    """
    mq.qimport(repo=repo, patch_file=patch_file)
    mq.qpush(repo=repo)
    message = mq.qheader(repo=repo)
    if not message:
        log.debug("No default message, using default")
        bz_bug = bz.get_bug(bug["bug_id"])
        message = generate_default_commit_message(bz_bug)
    message = generate_commit_message(message, patch, branch)

    # Don't override author in the patch by default
    user = None
    if not get_patch_user(patch_file):
        log.debug("No user defined in the patch, using Bugzilla user")
        user = author
    mq.qrefresh(repo=repo, message=message, user=user)


def generate_default_commit_message(bug):
    return "Bug %s - %s" % (bug["id"], bug["summary"])


def generate_commit_message(msg, patch, branch):
    """
    Handle the addition of a=... r=..., etc.
    """
    # Convert multiline messages into one line messages.
    # If we need to add support for multiline messages, we will need to switch
    # to --logfile syntax
    msg = msg.split("\n", 1)[0]
    msg = strip_reviews(msg)
    msg = add_reviews(msg, patch["reviews"])
    msg = add_approvals(msg, branch, patch["approvals"])
    return msg


def add_reviews(msg, reviews):
    # Bugzilla review type to hg message review mapping
    review_types = {
        'review': 'r',
        'superreview': 'sr',
        'ui-review': 'ui-r'
    }
    review_tail = ["%s=%s" % (review_types[r["type"]], r["reviewer"]["email"])
                   for r in reviews]
    review_tail = " ".join(review_tail)

    return "%s %s" % (msg, review_tail)


def add_approvals(msg, branch, approvals):
    branch_approvals = [a for a in approvals
                        if a['type'] == branch and a['result'] == '+']
    if branch_approvals:
        msg += " a=%s" % ','.join(a['approver']['email']
                                  for a in branch_approvals)
    return msg


def strip_reviews(message):
    """ Remove reviews/approvals from patch commit message """
    # reviews
    message = re.sub(r"\b(r|sr|ui-r)=[\S]+\s*", "", message)
    # approvals
    message = re.sub(r"\ba=[\S]+\s*", "", message)
    return message.strip()


def finish_import(repo, add_try_commit, try_syntax, email):
    """ Perform hg qfinish on applied patches """

    # Add a null commit with try syntax commit for branches with enabled try
    # syntax
    if add_try_commit:
        # create a null commit with try syntax
        message = 'try: %s' % try_syntax
        mq.qnew(repo=repo, message=message, user=email,
                queue_name="try_syntax")

    mq.qfinish(repo=repo)


def get_patch_user(patch_file):
    ph = patchheader(patch_file)
    return ph.user


def setup_repo(repo, pull_repo):
    # clean up the repo to make sure that we don't create "secret" changesets
    # in the hg share directory
    mq.clean_repo(repo, pull_repo)
    retry(mercurial, kwargs=dict(repo=pull_repo, dest=repo))


def import_patches(bug, branch_name, patch_ids, patches_dir, repo,
                   add_try_commit, try_syntax, pull_repo):
    setup_repo(repo=repo, pull_repo=pull_repo)
    # use email of last patch for try pushes
    try_committer = None
    for patch in bz.get_patches(bug_id=bug["bug_id"], patch_ids=patch_ids):
        patch_file = bz.download_patch(patch_id=patch["id"], path=patches_dir)
        author = "%s <%s>" % (patch["author"]["name"],
                              patch["author"]["email"])
        try_committer = author
        import_patch(repo, patch, patch_file, bug, branch_name, author)
    finish_import(repo, add_try_commit, try_syntax, try_committer)


def import_and_push(repo, bug, branch_name, patch_ids, patches_dir,
                    add_try_commit, try_syntax, pull_repo, push_repo,
                    ssh_username, ssh_key):

    def changer(repo, attempt):
        log.debug("Importing patches, attemp #%s", attempt)
        import_patches(bug=bug, branch_name=branch_name, patch_ids=patch_ids,
                       patches_dir=patches_dir, repo=repo,
                       add_try_commit=add_try_commit,
                       pull_repo=pull_repo, try_syntax=try_syntax)

    setup_repo(repo=repo, pull_repo=pull_repo)
    apply_and_push(localrepo=repo, remote=push_repo, changer=changer,
                   max_attempts=10, ssh_username=ssh_username, ssh_key=ssh_key,
                   force=False)
    return get_revision(repo)
