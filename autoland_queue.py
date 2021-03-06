import site
site.addsitedir('vendor')
site.addsitedir('vendor/lib/python')

import re
import time
import os, sys
import logging
import datetime
import urllib2
import subprocess

from utils import common
BASE_DIR = common.get_base_dir(__file__)
config = common.get_configuration([os.path.join(BASE_DIR, 'config.ini')])

site.addsitedir(os.path.join(config['tools'], 'lib/python'))
from utils import mq_utils, bz_utils, ldap_utils
from utils.db_handler import DBHandler, PatchSet, Branch, Comment

# permissions log to track all granted/used permissions
plog = logging.getLogger('permissions')
PLOGHANDLER = logging.FileHandler(config['log_permissions'])

log = logging.getLogger()
LOGFORMAT = logging.Formatter(config['log_format'])
LOGHANDLER = logging.StreamHandler()    # log to stdout

# Number of times to retry posting a comment
COMMENT_RETRIES = 5

bz = bz_utils.bz_util(api_url=config['bz_api_url'],
                      attachment_url=config['bz_attachment_url'],
                      username=config['bz_username'],
                      password=config['bz_password'],
                      webui_url=config['bz_webui_url'],
                      webui_login=config['bz_webui_login'],
                      webui_password=config['bz_webui_password'])
ldap = ldap_utils.ldap_util(config['ldap_host'],
                            int(config['ldap_port']),
                            branch_api=config['ldap_branch_api'],
                            bind_dn=config['ldap_bind_dn'],
                            password=config['ldap_password'])
db = DBHandler(config['databases_autoland_db_url'])

def get_reviews(attachment):
    """
    Takes attachment JSON, returns a list of reviews.
    Each review (in the list) is a dictionary containing:
        - Review type (review, superreview, ui-review)
        - Reviewer
        - Review Result (+, -, ?)
    """
    reviews = []
    if not 'flags' in attachment:
        return []
    for flag in attachment['flags']:
        if flag.get('name') in ('review', 'superreview', 'ui-review'):
            reviews.append({
                    'type':flag['name'],
                    'reviewer':bz.get_user_info(flag['setter']['name']),
                    'result':flag['status']
                    })
    return reviews

def get_approvals(attachment):
    """
    Takes attachment JSON, returns a list of approvals.
    Each approval (in the list) is a dictionary containing:
        - Approval type
        - Approver
        - Approval Result (+, -, ?)
    """
    print "Checking attachment"
    print attachment
    approvals = []
    app_re = re.compile(r'approval-')
    if not 'flags' in attachment:
        print "no flags"
        return approvals
    for flag in attachment['flags']:
        print "Flag: %s" % (flag)
        if app_re.match(flag.get('name')):
            approvals.append({
                    'type': app_re.sub('', flag.get('name')),
                    'approver':bz.get_user_info(flag['setter']['name']),
                    'result':flag['status']
                    })
    return approvals

def get_approval_status(patches, branch, perms):
    """
    Returns the approval status of the patchset for the given branch.
    Ensures that any passed approvals are also VALID approvals
        * The approval was given by someone with correct permission level
    If any patches failed approval, returns
        ('FAIL', [failed_patches])
    If any patches have invalid approval, returns
        ('INVALID', [invalid_patches])
    If any patches are still a? or have no approval flags,
        returns ('PENDING', [pending_patches])
    If all patches have at least one, and only passing approvals,
        returns ('PASS',)
    """
    if len(patches) == 0:
        return ('FAIL', None)
    failed = []
    invalid = []
    pending = []
    for patch in patches:
        approved = False
        p_id = patch['id']
        for app in patch['approvals']:
            if app['type'].strip().lower() != branch:
                continue
            if app['result'] == '+':
                # Found an approval, but keep on looking in case there is
                # a failed or pending approval.
                if common.in_ldap_group(ldap, app['approver']['email'], perms):
                    plog.info("PATCH %s: Approver %s has valid %s "
                              "permissions for branch %s"
                             % (p_id, app['approver']['email'], perms, branch))
                    approved = True
                elif p_id not in invalid:
                        plog.info("PATCH %s: Approver %s has invalid %s "
                                  "permissions for branch %s"
                             % (p_id, app['approver']['email'], perms, branch))
                        invalid.append(str(p_id))
            elif app['result'] == '?':
                if p_id not in pending: pending.append(str(p_id))
            else:
                # non-approval
                if p_id not in failed:
                    plog.info("PATCH %s: Approval failed for branch %s."
                        % (p_id, branch))
                    failed.append(str(p_id))
        if not approved:
            # There is no approval, so consider it pending.
            if p_id not in pending:
                plog.info("PATCH %s: No approval for branch %s."
                        % (p_id, branch))
                pending.append(str(p_id))

    if failed:
        return ('FAIL', failed)
    if invalid:
        return ('INVALID', invalid)
    if pending:
        return ('PENDING', pending)
    return ('PASS',)

def get_review_status(patches, perms):
    """
    Returns the review status of the patchset.
    Ensures that any passed reviews are also VALID reviews
        * The review was done by someone with the correct permission level
    If any patches failed review, returns
        ('FAIL', [failed_patches])
    If any patches have invalid review, returns
        ('INVALID', [invalid_patches])
    If any patches are still r? or have no review flags,
        returns ('PENDING', [pending_patches])
    If all patches have at least one, and only passing reviews,
        returns ('PASS',)
    """
    if len(patches) == 0:
        return ('FAIL', None)
    failed = []
    invalid = []
    pending = []
    for patch in patches:
        reviewed = False
        p_id = patch['id']
        for rev in patch['reviews']:
            if rev['result'] == '+':
                # Found a passed review, but keep on looking in case there is
                # a failed or pending review.
                if common.in_ldap_group(ldap, rev['reviewer']['email'], perms):
                    plog.info("PATCH %s: Reviewer %s has valid %s permissions"
                            % (p_id, rev['reviewer']['email'], perms))
                    reviewed = True
                elif p_id not in invalid:
                    plog.warn("PATCH %s: Reviewer %s "
                              "does not have %s permissions"
                            % (p_id, rev['reviewer']['email'], perms))
                    invalid.append(str(p_id))
            elif rev['result'] == '?':
                if p_id not in pending: pending.append(str(p_id))
            else:
                # non-review
                if p_id not in failed:
                    plog.info("PATCH %s: No passing review." % (p_id))
                    failed.append(str(p_id))
        if not reviewed:
            # There is no review on this, so consider it to be pending.
            if p_id not in pending:
                plog.info("PATCH %s: No review found." % (p_id))
                pending.append(str(p_id))

    if failed:
        return ('FAIL', failed)
    if invalid:
        return ('INVALID', invalid)
    if pending:
        return ('PENDING', pending)
    return ('PASS',)

def get_patches(bug_id, user_patches=None):
    """
    If user_patches specified, only fetch the information on those specific
    patches from the bug.
    If user_patches not specified, fetch the information on all patches from
    the bug.

    The returned patchset will return ALL patches, reviews, and approvals.

    Return value is of the JSON structure:
        [
            { 'id' : 54321,
              'author' : { 'name' : 'Name',
                           'email' : 'me@email.com' },
              'reviews' : [
                    { 'reviewer' : 'email',
                      'type' : 'superreview',
                      'result' : '+'
                    },
                    { ... }
                ],
              'approvals' : [
                    { 'approver' : 'email',
                      'type' : 'mozilla-beta',
                      'result' : '+'
                    },
                    { ... }
                ]
            },
            { ... }
        ]
    """
    patchset = []   # hold the final patchset information

    # grab the bug data
    bug_data = bz.get_bug(bug_id)
    if 'attachments' not in bug_data:
        return None     # bad bug id, or no attachments

    if user_patches:
        # user-specified patches, need to pull them in that set order
        user_patches = list(user_patches)    # take a local copy, passed by ref
        for user_patch in tuple(user_patches):
            for attachment in bug_data['attachments']:
                if attachment['id'] == user_patch and \
                        attachment['is_patch'] and not \
                        attachment['is_obsolete']:
                    patch = { 'id' : user_patch,
                          'author' :
                              bz.get_user_info(attachment['attacher']['name']),
                          'approvals' : get_approvals(attachment),
                          'reviews' : get_reviews(attachment) }
                    patchset.append(patch)
                    # remove the patch from user_patches to check all listed
                    # patches were pulled
                    user_patches.remove(patch['id'])
        if len(user_patches) != 0:
            # not all requested patches could be picked up
            # XXX TODO - should we still push what patches _did get picked up?
            log.debug('Autoland failure. Not all user_patches could '
                      'be picked up from bug.')
            post_comment(('Autoland Failure\nSpecified patches %s '
                          'do not exist, or are not posted to this bug.'
                          % (user_patches)), bug_id)
            return None
    else:
        # no user-specified patches, grab them in the order they were posted.
        for attachment in bug_data['attachments']:
            if attachment['is_patch'] and not attachment['is_obsolete']:
                patch = { 'id' : attachment['id'],
                          'author' : bz.get_user_info(
                              attachment['attacher']['name']),
                          'approvals' : get_reviews(attachment),
                          'reviews' : get_approvals(attachment) }
                patchset.append(patch)

    if len(patchset) == 0:
        post_comment('Autoland Failure\n There are no patches to run.', bug_id)
        patchset = None

    return patchset

def bz_search_handler():
    """
    Query Bugzilla WebService API for Autoland flagged bugs.
    For the moment, only supports push to try,
    and then to branch. It cannot push directly to branch.
    """
    bugs = []
    try:
        bugs = bz.autoland_get_bugs()
    except (urllib2.HTTPError, urllib2.URLError), err:
        log.error('Error while querying WebService API: %s' % (err))
        return
    if not bugs:
        return

    for bug in bugs:
        bug_id = bug['bug_id']

        # Grab the branches as a list, do a bit of cleaning
        branches = bug.get('branches', 'try')
        if not branches:
            log.info("Bug %s doesn't have any branches listed" % bug_id)
            continue
        branches = branches.split(',')
        branches = [x.strip() for x in branches]
        branches = [y for y in branches if y != '']
        branches.sort()

        for branch in tuple(branches):
            # clean out any invalid branch names
            # job will still land to any correct branches
            b = db.BranchQueryOne(Branch(name=branch))
            if b == None:
                branches.remove(branch)
                log.info('Branch %s does not exist.' % (branch))
                continue
            if b.status != 'enabled':
                branches.remove(branch)
                log.info('Branch %s is not enabled.' % (branch))
        if not branches:
            log.info('Bug %s had no correct branches flagged' % (bug_id))
# XXX: Update extension
            continue

        # the only patches that should be taken are the patches with status
        # 'waiting'
        patch_group = bug.get('attachments')
        # take only waiting patches
        patch_group = [x for x in patch_group if x['status'] == 'waiting']

        # check patch reviews & permissions
        patches = get_patches(bug_id, [x['id'] for x in patch_group])
        if not patches:
            # do not have patches to push, kick it out of the queue
# XXX UPDATE THE EXTENSION XXX
            log.error('No valid patches attached, nothing for '
                      'Autoland to do here, removing this bug from the queue.')
            continue

        patch_set = PatchSet(
            bug_id = bug_id,
            try_syntax = bug.get('try_syntax'),
            branch = ','.join(branches),
            patches = [x['id'] for x in patch_group],
            author = patch_group[0]['who'],
            try_run = True,
        )

        # get the branches
        comment = []
        for branch in tuple(branches):
            # clean out any invalid branch names
            # job will still land to any correct branches
            db_branch = db.BranchQueryOne(Branch(name=branch))
            if db_branch == None:
                branches.remove(branch)
                log.error('Branch %s does not exist.' % (branch))
                continue

            try:
                branch_perms = ldap.get_branch_permissions(branch)
            except ldap_utils.BranchDoesNotExist:
                comment.append("Branch %s does not exist." % (branch))
                branches.remove(branch)
                continue

            # check if branch landing r+'s are present
            # check branch name against try since branch on try iteration
            # will also have try_run set to True
            if branch.lower() != 'try':
                r_status = get_review_status(patches, branch_perms)
                if r_status[0] == 'FAIL':
                    cmnt = 'Review failed on patch(es): %s' \
                                % (' '.join(r_status[1]))
                    if cmnt not in comment:
                        comment.append(cmnt)
                    branches.remove(branch)
                    continue
                elif r_status[0] == 'PENDING':
                    cmnt = 'Review not yet given on patch(es): %s' \
                                    % (' '.join(r_status[1]))
                    if cmnt not in comment:
                        comment.append(cmnt)
                    branches.remove(branch)
                    continue
                elif r_status[0] == 'INVALID':
                    cmnt = 'Reviewer doesn\'t have correct ' \
                           'permissions for %s on patch(es): %s' \
                                % (branch, ' '.join(r_status[1]))
                    if cmnt not in comment:
                        comment.append(cmnt)
                    branches.remove(branch)
                    continue

            # check if approval granted on branch push.
            if db_branch.approval_required:
                a_status = get_approval_status(patches, branch, branch_perms)
                if a_status[0] == 'FAIL':
                    cmnt = 'Approval failed on patch(es): %s' \
                                    % (' '.join(a_status[1]))
                    if cmnt not in comment:
                        comment.append(cmnt)
                    branches.remove(branch)
                    continue
                elif a_status[0] == 'PENDING':
                    cmnt = 'Approval not yet given for branch %s ' \
                                   'on patch(es): %s' \
                                    % (branch, ' '.join(a_status[1]))
                    if cmnt not in comment:
                        comment.append(cmnt)
                    branches.remove(branch)
                    continue
                elif a_status[0] == 'INVALID':
                    cmnt = 'Approver for branch %s ' \
                                   'doesn\'t have correct ' \
                                   'permissions on patch(es): %s' \
                                    % (branch, ' '.join(a_status[1]))
                    if cmnt not in comment:
                        comment.append(cmnt)
                    branches.remove(branch)
                    continue

            # add the one branch to the database for landing
            job_ps = patch_set
            job_ps.branch = branch
            if db.PatchSetQuery(job_ps) != None:
                # we already have this in the db, don't run this branch
                comment.append('Already landing patches %s on branch %s.'
                                % (job_ps.patches, branch))
                branches.remove(branch)
                log.info('Duplicate patchset, removing branch: %s' % job_ps)
                continue

            # all runs will get a try_run by default for now
            # if it has a different branch listed, then it will do try run
            # then go to branch
            log.info('Inserting job: %s' % (job_ps))
            patchset_id = db.PatchSetInsert(job_ps)
            log.info('Insert Patchset ID: %s' % (patchset_id))

        if not branches:
            for patch in patch_set.patchList():
                bz.autoland_update_attachment(
                        {   'action':'remove',
                            'attach_id':patch   })
            comment.insert(0, 'Autoland Failure:')
        elif branches and comment:
            comment.insert(0, 'Autoland Warning:\n'
                              '\tOnly landing on branch(es): %s'
                               % (' '.join(branches)))
        if comment:
            post_comment('\n\t'.join(comment), bug_id)

@mq_utils.mq_util.generate_callback
def message_handler(message):
    """
    Handles json messages received. Expected structures are as follows:
    For a JOB:
        {
            'type' : 'JOB',
            'bug_id' : 12345,
            'branch' : 'mozilla-central',
            'try_run' : 1,
            'patches' : [ 53432, 64512 ],
        }
    For a SUCCESS/FAILURE:
        {
            'type' : 'ERROR',
            'action' : 'PATCHSET.APPLY',
            'patchsetid' : 123,
        }
    For try run PASS/FAIL:
        {
            'type' : 'SUCCESS',
            'action' : 'TRY.RUN',
            'revision' : '8dc05498d708',
        }
    """
    msg = message['payload']
    log.info('Received message:\n%s' % (message))
    if not 'type' in msg:
        log.error('Got bad mq message: %s' % (msg))
        return
    if msg['type'] == 'JOB':
        if 'try_run' not in msg:
            msg['try_run'] = True
        if 'bug_id' not in msg:
            log.error('Bug ID not specified.')
            return
        if 'branches' not in msg:
            log.error('Branches not specified.')
            return
        if 'patches' not in msg:
            log.error('Patch list not specified')
            return
        if not msg['try_run']:
            # XXX: Nothing to do, don't add.
            log.error('ERROR: try_run not specified.')
            return

        if msg['branches'].lower() == ['try']:
            msg['branches'] = ['mozilla-central']
            msg['try_run'] = True

        patch_set = PatchSet(bug_id=msg.get('bug_id'),
                      branch=msg.get('branch'),
                      try_run=msg.get('try_run'),
                      try_syntax=msg.get('try_syntax'),
                      patches=msg.get('patches')
                     )
        patchset_id = db.PatchSetInsert(patch_set)
        log.info('Insert PatchSet ID: %s' % (patchset_id))

    # attempt comment posting immediately, no matter the message type
    comment = msg.get('comment')
    if comment:
        # Handle the posting of a comment
        bug_id = msg.get('bug_id')
        if not bug_id:
            log.error('Have comment, but no bug_id')
        else:
            post_comment(comment, bug_id)

    if msg['type'] == 'SUCCESS':
        if msg['action'] == 'TRY.PUSH':
            # Successful push, add corresponding revision to patchset
            patch_set = db.PatchSetQuery(PatchSet(ps_id=msg['patchsetid']))
            if not patch_set:
                log.error('No corresponding patch set found for %s'
                        % (msg['patchsetid']))
                return
            patch_set = patch_set[0]
            for patch in patch_set.patchList():
                bz.autoland_update_attachment(
                        {   'action':'remove',
                            'attach_id':patch   })
            log.debug('Got patchset back from db: %s' % (patch_set))
            patch_set.revision = msg['revision']

            bz.autoland_update_attachment(
                    {   'action':'remove',
                        'attach_id':patch   })
            db.PatchSetUpdate(patch_set)
            log.debug('Added revision %s to patchset %s'
                    % (patch_set.revision, patch_set.id))

        elif '.RUN' in msg['action']:
            # this is a result from schedulerdbpoller
            patch_set = db.PatchSetQuery(PatchSet(revision=msg['revision']))
            if not patch_set:
                log.error('Revision %s not found in database.'
                        % (msg['revision']))
                return
            patch_set = patch_set[0]
            # is this the try run before push to branch?
            if patch_set.try_run and \
                    msg['action'] == 'TRY.RUN' and patch_set.branch != 'try':
                # remove try_run, when it comes up in the queue
                # it will trigger push to branch(es)
                patch_set.try_run = False
                patch_set.push_time = None
                log.debug('Flag patchset %s revision %s for push to branch.'
                        % (patch_set.id, patch_set.revision))
                db.PatchSetUpdate(patch_set)
            else:
                # close it!
                db.PatchSetComplete(patch_set, status="SUCCESS: Try run complete")
                log.debug('Deleting patchset %s' % (patch_set.id))
                return

        elif msg['action'] == 'BRANCH.PUSH':
            # Guaranteed patchset EOL
            patch_set = db.PatchSetQuery(PatchSet(ps_id=msg['patchsetid']))[0]
            # XXX: If eventually able to land without try push, this may
            #      need to update the extension.
            patch_set.revision = msg.get('revision')
            db.PatchSetComplete(patch_set,
                    status="SUCCESS: Pushed to branch.")
            log.debug('Successful push to branch of patchset %s.'
                    % (patch_set.id))
    elif msg['type'] == 'TIMED_OUT':
        patch_set = None
        if msg['action'] == 'TRY.RUN':
            patch_set = db.PatchSetQuery(PatchSet(revision=msg['revision']))
            if not patch_set:
                log.error('No corresponding patchset found '
                        'for timed out revision %s' % msg['revision'])
                return
            patch_set = patch_set[0]
        if patch_set:
            # remove it from the queue, timeout should have been comented
            db.PatchSetComplete(patch_set, status="Try run timed out.")
            log.debug('Received time out on %s, deleting patchset %s'
                    % (msg['action'], patch_set.id))
    elif msg['type'] == 'ERROR' or msg['type'] == 'FAILURE':
        patch_set = None
        if msg['action'] == 'TRY.RUN' or msg['action'] == 'BRANCH.PUSH':
            patch_set = db.PatchSetQuery(PatchSet(revision=msg['revision']))
            if not patch_set:
                log.error('No corresponding patchset found for revision %s'
                        % (msg['revision']))
                return
            patch_set = patch_set[0]
        elif msg['action'] == 'PATCHSET.APPLY':
            patch_set = db.PatchSetQuery(PatchSet(ps_id=msg['patchsetid']))
            if not patch_set:
                # likely an untracked patch set sent from schedulerdbpoller
                log.error('No corresponding patchset found for patch set id %s'
                        % msg['patchsetid'])
                return
            patch_set = patch_set[0]

        if patch_set:
            # remove it from the queue, error should have been comented to bug
            db.PatchSetComplete(patch_set, status="FAILURE:An error occurred.")
            log.debug('Received error on %s, deleting patchset %s'
                    % (msg['action'], patch_set.id))
            for patch in patch_set.patchList():
                bz.autoland_update_attachment(
                        {   'action':'remove',
                            'attach_id':patch   })

def handle_patchset(mq, patchset):
    """
    Message sent to HgPusher is of the JSON structure:
        {
          'job_type' : 'patchset',
          'bug_id' : 12345,
          'branch' : 'mozilla-central',
          'push_url' : 'ssh://hg.mozilla.org/try',
          'branch_url' : 'ssh://hg.mozilla.org/mozilla-central',
          'try_run' : 1,
          'try_syntax': '-p linux -u mochitests',
          'patchsetid' : 42L,
          'patches' :
                [
                    { 'id' : 54321,
                      'author' : { 'name' : 'Name',
                                   'email' : 'me@email.com' },
                      'reviews' : [
                            { 'reviewer' : { 'name' : 'Rev. Name',
                                             'email' : 'rev@email.com' },
                              'type' : 'superreview',
                              'result' : '+'
                            },
                            { ... }
                        ],
                      'approvals' : [
                            { 'approver' : { 'name' : 'App. Name',
                                             'email' : 'app@email.com' },
                              'type' : 'mozilla-esr10',
                              'result' : '+'
                            }
                        ]
                    },
                    { ... }
                ]
        }
    """
    log.debug('Handling patchset %s from queue.' % (patchset))

    # TODO: Check the retries & creation time.

    # Check permissions & patch set again, in case it has changed
    # since the job was put on the queue.
    patches = get_patches(patchset.bug_id, user_patches=patchset.patchList())
    if patches == None:
        # Comment already posted in get_patches. Full patchset couldn't be
        # processed.
        log.info("Patchset not valid. Deleting from database.")
        db.PatchSetComplete(patchset, status="FAILUE: Invalid Patch Set")
        return

    # get branch information
    branch = db.BranchQueryOne(Branch(name=patchset.branch))
    if not branch:
        # error, branch non-existent
        # XXX -- Should we email or otherwise let user know?
        log.error('Could not find %s in branches table.' % (patchset.branch))
        db.PatchSetComplete(patchset, status="FAILURE: Branch is not supported.")
        return

    try:
        branch_perms = ldap.get_branch_permissions(branch.name)
    except ldap_utils.BranchDoesNotExist:
        post_comment("Autoland Failure:\n"
                     "Cannot land to branch %s.\n"
                     "Branch %s does not exist." % (branch, branch))
        return

    # double check if this job should be run
    if patchset.branch.lower() != 'try':
        r_status = get_review_status(patches, branch_perms)
        if r_status[0] != 'PASS':
            log.info('%s review on patches %s'
                        % (r_status[0], ','.join(r_status[1])))
            if r_status[0] == 'FAIL':
                comment = 'Autoland Failure:\n' \
                          '%sFailed review on patch(es): %s' \
                                % (' '.join(r_status[1]))
            elif r_status[0] == 'PENDING':
                comment = 'Autoland Failure:\n' \
                          'Missing required review for patch(es): %s' \
                                % (' '.join(r_status[1]))
            elif r_status[0] == 'INVALID':
                comment = 'Autoland Failure:\n' \
                          'Invalid review for patch(es): %s' \
                                % (' '.join(r_status[1]))
            db.PatchSetComplete(patchset, status=comment)
            post_comment(comment, patchset.bug_id)
            return
    if branch.approval_required:
        a_status = get_approval_status(patches, patchset.branch, branch_perms)
        if a_status[0] != 'PASS':
            log.info('%s approval on patches %s for branch %s'
                    % (r_status[0], ','.join(r_status[1]), patchset.branch))
            if a_status[0] == 'FAIL':
                comment = 'Autoland Failure:\n' \
                          'Failed approval for branch %s on patch(es): %s' \
                                % (patchset.branch, ' '.join(a_status[1]))
            elif a_status[0] == 'PENDING':
                comment = 'Autoland Failure:\n' \
                          'Missing required approval for branch %s ' \
                          'on patch(es): %s' \
                                % (patchset.branch, ' '.join(a_status[1]))
            elif r_status[0] == 'INVALID':
                comment = 'Autoland Failure:\n' \
                          'Invalid approval for patch(es): %s' \
                                % (' '.join(a_status[1]))
            db.PatchSetComplete(patchset, status=comment)
            post_comment(comment, patchset.bug_id)
            return

    if patchset.try_run:
        running = db.BranchRunningJobsQuery(Branch(name='try'))
        log.debug("Running jobs on try: %s" % (running))

        # get try branch info
        try_branch = db.BranchQueryOne(Branch(name='try'))
        if not try_branch: return

        log.debug("Threshold for try: %s" % (try_branch.threshold))

        # ensure try is not above threshold
        if running >= try_branch.threshold:
            log.info("Too many jobs running on try right now.")
            return
        push_url = try_branch.repo_url
    else:   # branch landing
        running = db.BranchRunningJobsQuery(Branch(name=patchset.branch),
                                            count_try=False)
        log.debug("Running jobs on %s: %s" % (patchset.branch, running))

        log.debug("Threshold for branch: %s" % (branch.threshold))

        # ensure branch not above threshold
        if running >= branch.threshold:
            log.info("Too many jobs landing on %s right now." % (branch.name))
            return
        push_url = branch.repo_url

    message = { 'job_type' : 'patchset', 'bug_id' : patchset.bug_id,
            'branch_url' : branch.repo_url,
            'push_url' : push_url, 'user' : patchset.author,
            'branch' : patchset.branch, 'try_run' : patchset.try_run,
            'try_syntax' : patchset.try_syntax,
            'patchsetid' : patchset.id, 'patches' : patches }

    for patch in patchset.patchList():
        bz.autoland_update_attachment(
                {   'action' : 'status',
                    'status' : 'running',
                    'attach_id' : patch   })
    patchset.push_time = datetime.datetime.utcnow()
    db.PatchSetUpdate(patchset)
    log.info("Sending job to hgpusher: %s" % (message))
    mq.send_message(message, routing_key='hgpusher')

def handle_comments():
    """
    Queries the Autoland db for any outstanding comments to be posted.
    Gets the five oldest comments and tries to post them on the corresponding
    bug. In case of failure, the comments attempt count is updated, to be
    picked up again later.
    If we have attempted 5 times, get rid of the comment and log it.
    """
    comments = db.CommentGetNext(limit=5)   # Get up to 5 comments
    for comment in comments:
        # Note that notify_bug makes multiple retries
        success = bz.notify_bug(comment.comment, comment.bug)
        if success:
            # Posted. Get rid of it.
            db.CommentDelete(comment)
        elif comment.attempts >= COMMENT_RETRIES:
            # 5 attempts have been made, drop this comment as it is
            # probably not going anywhere.
            try:
                with open('failed_comments.log', 'a') as fc_log:
                    fc_log.write('%s\n\t%s\n' % (comment.bug, comment.comment))
            except IOError:
                log.error('Unable to append to failed comments file.')
            log.error("Could not post comment to bug %s. Dropping comment: %s"
                    % (comment.bug, comment.comment))
            db.CommentDelete(comment.id)
        else:
            comment.attempts += 1
            db.CommentUpdate(comment)

def post_comment(comment, bug_id):
    """
    Post a comment that isn't in the comments db.
    Add it if posting fails.
    """
    success = bz.notify_bug(comment, bug_id)
    if success:
        log.info('Posted comment: "%s" to %s' % (comment, bug_id))
    else:
        log.info('Could not post comment to bug %s. Adding to comments table'
                % (bug_id))
        cmnt = Comment(comment=comment, bug=bug_id)
        if not db.CommentInsert(cmnt):
            log.error("Unable to insert comment %s. Wont't be posted.", cmnt)
            try:
                with open('failed_comments.log', 'a') as fc_log:
                    fc_log.write('%s\n\t%s\n' % (comment.bug, comment.comment))
            except IOError:
                log.error('Unable to append to failed comments file.')


def main():
    mq = mq_utils.mq_util(host=config['mq_host'],
                          vhost=config['mq_vhost'],
                          username=config['mq_username'],
                          password=config['mq_password'],
                          exchange=config['mq_exchange'])
    mq.connect()
    mq.declare_and_bind(config['mq_autoland_queue'], 'db')

    log.setLevel(logging.INFO)
    LOGHANDLER.setFormatter(LOGFORMAT)
    log.addHandler(LOGHANDLER)

    PLOGHANDLER.setLevel(logging.INFO)
    PLOGHANDLER.setFormatter(LOGFORMAT)
    plog.addHandler(PLOGHANDLER)

    # XXX: use argparse
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg == '--purge-queue':
                # purge the autoland queue
                mq.purge_queue(config['mq_autoland_queue'], prompt=True)
                exit(0)
            elif arg == '--debug' or arg == '-d':
                log.setLevel(logging.DEBUG)

    while True:
        # search bugzilla for any relevant bugs
        bz_search_handler()
        next_poll = time.time() + int(config['bz_poll_frequency'])

        # take care of any comments that couldn't previously be posted
        handle_comments()

        while time.time() < next_poll:
            patchset = db.PatchSetGetNext()
            if patchset != None:
                handle_patchset(mq, patchset)

            # loop while we've got incoming messages
            while mq.get_message(config['mq_autoland_queue'], message_handler):
                continue
            time.sleep(5)

if __name__ == '__main__':
    main()

