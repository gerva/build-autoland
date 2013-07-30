import urllib2
import httplib

HTTP_EXCEPTIONS = (urllib2.HTTPError, urllib2.URLError, httplib.BadStatusLine)


class BranchDoesNotExist(Exception):
    pass


class ReviewException(Exception):
    FAIL, INVALID, PENDING = range(3)

    def __init__(self, status, reason=None):
        self.status = status
        self.reason = reason
        super(ReviewException, self).__init__(self)


class ApprovalException(ReviewException):
    pass


class InvalidAttachment(Exception):
    pass


class DataError(Exception):
    pass
