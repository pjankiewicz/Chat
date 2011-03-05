#!/usr/bin/env python
# -*- coding: utf-8 -*-


import logging
import time

from google.appengine.ext import webapp
from google.appengine.api import users, channel

from google.appengine.ext.webapp.util import run_wsgi_app
from google.appengine.ext.webapp import template

from tournament2 import DeathMatch, Player

class BaseHandler (webapp.RequestHandler):
    template_values = {}

    def render (self, path):
        self.template_values.update ({'logout_url' : users.create_logout_url ('/bye')})
        return self.response.out.write (template.render (path,
                                    self.template_values))

class MainPage (BaseHandler):
    
    def get (self):
        userid = users.get_current_user ().user_id ()
        player = Player.from_id (userid)
        if player.is_playing:
            self.template_values.update ({'gamekey' : player.game_in })
        clientid_for_channel = player.create_channel ()
        token = channel.create_channel (clientid_for_channel)
        self.template_values.update ({'token' : token,
                                    'userid' : player.keyname})
        return self.render ('index.html')

class JoinGame (BaseHandler):
    def post (self):
        """
        Process the req from a client/player to join a room
        """
        userid = users.get_current_user ().user_id ()
        player = Player.from_id (userid)
        if not player.is_playing:
            return DeathMatch.join_latest_or_new (player)
        return DeathMatch.resume (player)


class Chat (BaseHandler):
    def post (self):
        userid = users.get_current_user ().user_id ()
        message = self.request.get ('m', '')
        logging.info (message)
        Player.from_id (userid).chat (message)


application = webapp.WSGIApplication(
                            [('/', MainPage),
                            ('/joingame.*', JoinGame),
                            ('/chat', Chat),
                            # ('/bye', Bye),
                            # ('/leave', Leave),
                            ],
                            debug=True)

def main():
    run_wsgi_app(application)


if __name__ == "__main__":
    main()       
