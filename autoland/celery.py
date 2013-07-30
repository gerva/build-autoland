from __future__ import absolute_import
from celery import Celery
import autoland.tasks


celery = Celery('autoland')
celery.config_from_object("celeryconfig")
