from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

from autoland.config import config


def str_to_datetime(str_datetime):
    """ Convert Bugzilla retruned datetime string into Python datetime """
    return datetime.strptime(str_datetime, '%Y-%m-%dT%H:%M:%SZ')


class AutolandDB(object):

    def __init__(self):
        self.session = None
        self.metadata = None
        self.engine = None

    def initialize(self, engine_url):
        self.engine = create_engine(engine_url)
        Session = sessionmaker(bind=self.engine)
        self.session = Session()


Base = declarative_base()
db = AutolandDB()
db.initialize(config["db_engine_url"])
