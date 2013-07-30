import ConfigParser

CONF_FILES = ["autoland.ini", "/etc/autoland.ini"]


class Config(dict):

    def read(self, conf_files):
        config = ConfigParser.RawConfigParser()
        config.read(conf_files)
        for section in config.sections():
            for item in config.items(section):
                if section != 'defaults':
                    key = '%s_%s' % (section, item[0])
                else:
                    key = item[0]
                self.__setitem__(key, item[1])


config = Config()
config.read(CONF_FILES)
