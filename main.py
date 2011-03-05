#!/usr/bin/env python
# -*- coding: utf-8 -*-

from google.appengine.api import users
from google.appengine.ext import webapp
from google.appengine.api import channel
from google.appengine.ext.webapp.util import run_wsgi_app
from google.appengine.ext.webapp import template

from tournament import Game, Player

class BaseHandler(webapp.RequestHandler):
    template_values = {}

    def render(self, path):
        self.template_values.update({'logout_url' : users.create_logout_url('/bye')})
        return self.response.out.write(template.render(path,
                            self.template_values))


class MainPage(BaseHandler):
    def get(self):
        userid = users.get_current_user().user_id()
        player = Player.from_id(userid)
        if player.gameroom is not None:
            self.template_values.update({'gamekey': player.gameroom})
        clientid_for_channel = player.create_channel()
        token = channel.create_channel(clientid_for_channel)
        self.template_values.update({'token' : token,
                            'userid' : player.keyname})
        return self.render('index.html')


class JoinGame(BaseHandler):
    def post(self):
        """
        Processes the req from a client/player to join a gameroom
        """
        user = users.get_current_user()
        if not user:
            return self.redirect(users.create_login_url('/'))
        player = Player.from_id(user.user_id())
        if not player.gameroom:
            return Game.join_latest_or_new(player)
        return Game.continue_tournament(player)


class Chat(BaseHandler):
    """
    Chatting between users
    """
    def post(self):
       userid = users.get_current_user().user_id()
       message = self.request.get('m', '')
       Player.from_id(userid).chat(message)


class Bye(BaseHandler):
    """
    Handles the Bye
    """
    def get(self):
        self.response.out.write("""<html><body>
                Bye. It was nice to see you here !!<a href="/">Login Again</a>
                </body></html>""")

class Leave(BaseHandler):
    """
    Handles when the player decides to leave the gameroom
    """
    def get(self):
        userid = users.get_current_user().user_id()
        Player.from_id(userid).leave_tournament()
        self.redirect('/bye')

application = webapp.WSGIApplication(
                            [('/', MainPage),
                             ('/joingame.*', JoinGame),
                             ('/chat', Chat),
                             ('/bye', Bye),
                             ('/leave', Leave),],
                            debug=True)

def main():
    run_wsgi_app(application)


if __name__ == "__main__":
    main()
