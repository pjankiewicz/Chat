import cgi

from google.appengine.api import users
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app
from google.appengine.ext import db

class ChatLog(db.Model):
    author = db.StringProperty()
    room = db.StringProperty()
    text = db.TextProperty(multiline=True)
    date = db.DateTimeProperty(auto_now_add=True)
