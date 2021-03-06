import ConfigParser
import urllib2, httplib
import os

HTTP_EXCEPTIONS = (urllib2.HTTPError, urllib2.URLError, httplib.BadStatusLine)

TBPL_NAMES = {
    'mozilla-central' : 'Firefox',
    'try' : 'Try',
    'mozilla-inbound' : 'Mozilla-Inbound',
    'mozilla-aurora' : 'Mozilla-Aurora',
    'mozilla-beta' : 'Mozilla-Beta',
    'mozilla-release' : 'Mozilla-Release',
    'mozilla-esr10' : 'Mozilla-Esr10',
}

def get_configuration(conf_files):
    # load configuration
    config = ConfigParser.RawConfigParser()
    for conf_file in conf_files:
        config.read(conf_file)
    cfg = {}
    for section in config.sections():
        for item in config.items(section):
            if section != 'defaults':
                key = '%s_%s' % (section, item[0])
            else:
                key = item[0]
            cfg[key] = item[1]
    return cfg

def get_base_dir(path):
    return os.path.abspath(os.path.dirname(os.path.realpath(path)))

def in_ldap_group(ldap, email, group):
    """
    Checks ldap if either email or the bz_email are a member of the group.
    """
    bz_email = ldap.get_bz_email(email)
    return ldap.is_member_of_group(email, group) \
            or (bz_email and ldap.is_member_of_group(bz_email, group))

