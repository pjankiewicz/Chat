#!/usr/bin/env python
# -*- coding: utf-8 -*-

__author__ = 'Pranav Prakash < x@pranavprakash.in>'

#	This is a demo application about using the Channel API in 
#	Google App Engine. This application does chat and some real
#	actions between a set of users in a room.
#
#	Copyright (C) 2010-2011  Pranav Prakash
#
#	This program is free software: you can redistribute it and/or modify
#	it under the terms of the GNU General Public License as published by
#	the Free Software Foundation, either version 3 of the License, or
#	(at your option) any later version.
#
#	This program is distributed in the hope that it will be useful,
#	but WITHOUT ANY WARRANTY; without even the implied warranty of
#	MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#	GNU General Public License for more details.
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.


import md5
import logging
import time, random

from google.appengine.api import channel
from google.appengine.api import users
from google.appengine.api import memcache

from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app
from google.appengine.ext.webapp import template

from google.appengine.ext.db import TransactionFailedError

from google.appengine.datastore import entity_pb

from django.utils import simplejson

#	The maximum number of participants in the event
#	Keep it -1 for unlimited access

MAX_PARTICIPANTS = 2
LATEST_GAMEROOM = 'latest_gameroom'

#   The maximum difference in revisions acceptable  at any instant
#   between memcached values and the datastore values. The higher it
#   is, the greater catastrophe when memcache goes down, but lesser
#   datastore usage. The lesser it is, the more consistent your datastore
#   and memcache are, and higher datastore operations. 4~6
FAULT_TOLERANCE = 4

#	There are two entity kinds - Game and PlayerGame
#
#	The Game Entity Kind contains information about the game room
#	This is where you can keep a track of all the events happening
#	in a room like maybe chat between users, or collaborative
#	drawing or the moves in a tic-tac-toe.
#
#	The PlayerGame is a child entity of the Game entity, which contains
#	the information about the game the user is playing. For those games 
#	which go in a sequential manner where player1 does something and then
#	player2 does something, PlayerGame could have very well been inside
#	the Game entity, but in a game where many players can do multiple
#	actions and their actions affect their state in some way or the other
#	datastore congestion might occur. To avoid all those, its made in
#	a separate entity.

#   UPDATE
#   This is the third major architectural change in the way this module
#   functions. In this change, memcache is added for reducing latency
#   and revision numbers are added to make sure that events are always
#   ordered and any conflict can be smartly dealth with
#   

#   ISSUES
#   1. Since the mechanisam used to create channels for a player is directly
#   dependent on the player's UserID hence logging in from multiple clients
#   creates issues. If the first client from where the player logged in is 
#   primary client and rest all are secondary clients, the secondary client
#   is able to send actions but do not receieve updates
#
#   2. 

class Game(db.Model):
    """
        The Model to store the details of a particular
        game room.
    """
    players = db.StringListProperty()
    chat    = db.TextProperty()
    active  = db.BooleanProperty(default = True)
    revision= db.IntegerProperty(default = 0)
    version_in_db = db.IntegerProperty(default = 0)
    created = db.DateTimeProperty(auto_now_add = True)
    updated = db.DateTimeProperty(auto_now = True)
    
class PlayerGame(db.Model):
    """
        Stores information about which player is added into which game
        room. This of course assumes that a player can be in only one
        game room at a time.
    """
    assets   = db.TextProperty(default = '')
    name     = db.StringProperty()
    channels = db.StringListProperty()
    revision = db.IntegerProperty(default = 0)
    version_in_db = db.IntegerProperty(default = 0)
    created  = db.DateTimeProperty(auto_now_add = True)
    updated  = db.DateTimeProperty(auto_now = True)

#   Memcaching of entities effeciently
#   http://blog.notdot.net/2009/9/Efficient-model-memcaching

def serialize_entities(models):
    if models is None:
        return None
    elif isinstance(models, db.Model):
        return db.model_to_protobuf(models).Encode()
    else:
        return [db.model_to_protobuf(x).Encode() for x in models]

def deserialize_entities(data):
    if data is None:
        return None
    elif isinstance(data, str):
        return db.model_from_protobuf(entity_pb.EntityProto(data))
    else:
        return [db.model_from_protobuf(entity_pb.EntityProto(x)) for x in data]

def gen_channel(userid):
    seed = userid + str(int(time.time()))
    return md5.md5(seed).hexdigest()

class Player(object):
    """
    A Player class
    """
    userid = None
    assets = ''
    name = ''
    updates = {}
    _player = None
    
    def __init__(self, userid=None):
        self.userid = userid
    
    def _get_tournament(self):
        if self.userid:
            return Game.all(keys_only=True).filter('players = ', self.userid).get()
    
    def get_player(self):
        logging.info('_player: %s, userid: %s' %(self._player.__class__, self.userid))
        if self._player is not None:
            return self._player
        if self.userid is not None:
            logging.info('Attempting to fetch player info from memcache')
            self._player = deserialize_entities(memcache.get(self.userid))
            if self._player is None:
                logging.info('Attempting to fetch player info from datastore')
                self._player = PlayerGame.get_by_key_name(self.userid,
                                            parent = self._get_tournament())
            return self._player
    
    def _store(self, player):
        "Stores the player object"
        self._player = player
        if self._player.revision is None:
            self._player.revision = 0
        self._player.revision += 1
        logging.info('Setting memcache. Key: %s' %(self.userid))
        memcache.set(self.userid, serialize_entities(self._player))
        self._check_and_sync()
    
    def _check_and_sync(self):
        "Checks and sync the memcache and datastore"
        if self._player.revision - self._player.version_in_db >= FAULT_TOLERANCE:
            self.sync_to_db()
        if self._player.revision < self._player.version_in_db:
            self.sync_with_db()
    
    def sync_to_db(self):
        "Update the db values with that of memcache"
        player = self.get_player()
        logging.info('Attempting Player sync_to_db')
        if player is not None and player.revision - player.version_in_db >=  FAULT_TOLERANCE:
            player.version_in_db = player.revision
            memcache_details = deserialize_entities(memcache.get(player.userid))
            db.put(memcache_details)
            self._store(player)
            logging.info('Player Sync to db complete')
            return True
        logging.info('Conditions not met for initiate Player sync to db')
        return False
    
    def sync_with_db(self):
        "Updates the memcache with values that of datastore"
        pass
    
    def get_channel(self):
        another_channel = gen_channel(self.userid)
        player = self.get_player()
        player.channels.append(another_channel)
        self._store(player)
        return another_channel 
   
    @property
    def active_channels(self):
        "Always returns a list of channel IDs this player is on"
        p = self.get_player()
        channels = p.channels
        if isinstance(channels, list): return channels
        if isinstance(channels, str): return [channels]

    def get_gameroom(self):
        pg = self.get_player()
        logging.info('playergame: %s' %pg)
        if pg:
            logging.info(pg.parent().key().name())
            return pg.parent().key().name()
        # maybe, if the player does not have any room,
        # allot him to the latest empty room
            
    def die(self):
        playergame_key = db.Key.from_path('PlayerGame', self.userid)
        game = Game.all().filter('user = ', self.userid).get()
        
        def txn(userid, pg_key, g):
            db.delete(pg_key)
            g.users.pop(userid)
            db.put(g)
        
        memcache.delete(self.userid)
        db.run_in_transaction(txn, self.userid, playergame_key, game_key)
    
    def get_assets(self):
        return self.assets
    
    def grant_default_assets(self):
        pass
    
    def do_action(self, action):
        "So some 'action' while in the room"
        #    Modify the player's assets accordingly
        #    player = self.get_player()
        #    db.put(player)
        pass


class Tournament(object):
    """
        A better Tournament Class which uses memcache heavily for faster
        performance and does not loose grip on consistency of data
    """
    game = None
    room = None
    delta_chat = ''
    updates = {}
    
    def __init__(self, room=None):
        self.room = room
        self.updates = {}
    
    @property
    def _memcache_key(self):
        return 'tournament_' + str(self.room)
   
    def get_tournament(self):
        "Returns the Game object for this tournament"
        if self.game:
            return self.game
        if self.room:
            self.game = deserialize_entities(memcache.get(self._memcache_key))
            logging.info('Fetching game from memcache')
            if not self.game:
                logging.info('Game not in the memcache...Memcache Key: %s' %self._memcache_key)
                self.game = Game.get_by_key_name(self.room)
            if self.game:
                logging.info('Setting memcache. Key: %s' %(self._memcache_key))
                memcache.set(self._memcache_key, serialize_entities(self.game))
                return self.game
        logging.info('No Game Object found ...')
    
    def can_add_player(self):
        "Check if more players can be added to this room"
        if MAX_PARTICIPANTS == -1:
            return True
        return len(self.get_tournament().players) < MAX_PARTICIPANTS
   
    def _store(self, game):
        "Stored the game object given in memcache"
        self.game = game
        if self.game.revision is None:
            self.game.revision = 0
        self.game.revision += 1
        logging.info('Setting memcache. Key: %s' %(self._memcache_key))
        memcache.set(self._memcache_key, serialize_entities(self.game))
        self._check_and_sync()
    
    def _check_and_sync(self):
        "Checks if its the need to sync datastore and memcache and does the honor"
        if self.game.revision - self.game.version_in_db >= FAULT_TOLERANCE:
            return self.sync_to_db()
        if self.game.revision < self.game.version_in_db:
            return self.sync_from_db()
    
    def sync_to_db(self):
        "Updates datastore based on memcache"
        game = self.get_tournament()
        logging.info('Attempting sync_to_db')
        if game is not None and game.revision - game.version_in_db >=  FAULT_TOLERANCE:
            game.version_in_db = game.revision
            memcache_keys_to_fetch = [self._memcache_key]
            memcache_keys_to_fetch.extend(game.players)
            memcached_values = memcache.get_multi(memcache_keys_to_fetch)
            memcache_details = deserialize_entities(memcached_values.values())
            db.put(memcache_details)
            self._store(game)
            logging.info('Sync to db complete')
            return True
        logging.info('Conditions not met for initiate sync to db')
        return False
    
    def sync_from_db(self):
        "Updates memcache based on datastore"
        game = self.get_tournament()
        logging.info('Attempting sync from db')
        if game is not None and game.revision < game.version_in_db:
            game.revision = game.version_in_db
            player_keys = [db.Key.from_path('Game', 'PlayerGame', x) for x in game.players]
            memcache_values = serialize_entities(db.get([game.key()].extend(player_keys)))
            memcache_dict = { self._memcache_key : memcache_values[0] }
            for x in game.players:
                memcache_dict[x] = memcache_values[game.players.index(x)]
            memcache.set_multi(memcache_dict)
            self._store(game)
            logging.info('Sync from db complete')
            return True
        logging.info('Conditions not met to initiate sync from db')
        return False
       
    def sync(self):
        "Tries to figure out which is latest and updates the older one"
        raise NotImplementedError


    def add_player(self, player):
        "add_player(player) -> Add a 'player' to the tournament."
        if self.can_add_player():
            game = self.get_tournament()
            if player.userid not in game.players:
                game.players.append(player.userid)
                self._store(game)
            p = PlayerGame.get_or_insert(key_name = player.userid,
                                    parent = game)
            memcache.set(p.key().name(), serialize_entities(p))
            logging.info('Setting memcache. Key:%s'%(p.key().name()))
            self.updates.update({'new_player' : 1,
                                 'name' : player.userid,})
            self.send_update()
        else:
            return self.new(player)
    
    def new(self, player):
        "A new room shall be created only if there is a player to go into"
        if not player: return
        self.room = str(time.time())
        self.game = Game(key_name = self.room,
                players = [player.userid])
        playergame = PlayerGame(key_name = player.userid,
                        parent = self.game)
        def txn(game, playergame):
            db.put([game, playergame])
        try:
            db.run_in_transaction(txn, self.game, playergame)
            memcache_data_to_set = { LATEST_GAMEROOM    : self.room,
                                     self._memcache_key : serialize_entities(self.game),
                                     playergame.key().name() : serialize_entities(playergame),
                                    }
            memcache.set_multi(memcache_data_to_set)
            return self.room
        except TransactionFailedError:
            logging.info('Transaction Failed')
           
    @classmethod
    def join_latest_or_new(cls, player):
        "Join the latest tournament if available or create a new tournament"
        latest_roomkey = memcache.get(LATEST_GAMEROOM)
        if latest_roomkey:
            cls(latest_roomkey).add_player(player)
            return latest_roomkey
        return cls().new(player)
    
    @classmethod
    def continue_tournament(cls, player):
        "When the player was already in a tournament continue from there itself"
        logging.info('Sending message %s on channel %s' %(cls().get_all_updates(),
                          player.get_channel()))
        channel.send_message(player.get_channel(), cls().get_all_updates())
    
    def remove_player(self, player):
        "Marks the end of a player from the tournament"
        game = self.get_tournament()
        game.players.pop(player.userid)
        self._store(game)
        player.die()
    
    def chat(self, player, message):
        "Talk folks, talk !!"
        delta_chat = message
        game = self.get_tournament()
        if game.chat:
            chat = game.chat + '<br />' + delta_chat
        else:
            chat = delta_chat
        game.chat = chat
        self._store(game)
        self.updates.update({'delta_chat' : delta_chat})
        self.send_update()
    
    def end(self):
        "Marks the end of the tournament. End of the show"
        game = self.get_tournament()
        if game:
            player_keys = [db.Key.from_path('Game', 'PlayerGame', x) for x in game.players]
            memcache_keys_to_delete = [self._memcache_key]
            memcache_keys_to_delete.extend(player_keys)
            memcache.delete_multi(memcache_keys_to_delete)
            logging.info('memcache cleared for this game')
            db.delete([game.key()].extend(player_keys))
            return True
    
    def _create_channel(self, userid):
        "Create a channel for the userid"
        return gen_channel(userid, self.room)
    
    def get_player_channels(self):
        "Finds out all the channels associated with this tournament"
        game = self.get_tournament()
        return [Player(x).get_channel() for x in game.players]
    
    def get_game_message(self):
        update = self.updates
        return simplejson.dumps(update)
    
    def get_all_updates(self):
        "Collects all the updates"
        update = self.updates
        update.update({'all' : 'all'})
        return simplejson.dumps(update)
    
    def send_update(self):
        "Sends an update on all channels for this game"
        message = self.get_game_message()
        for channel_id in self.get_player_channels():
            channel.send_message(channel_id, message)
    
class BaseHandler(webapp.RequestHandler):
    template_values = {}
    
    def render(self, path):
        self.template_values.update({'logout_url' : users.create_logout_url('/bye')})
        return self.response.out.write(template.render(path, 
                            self.template_values))

class MainPage(BaseHandler):
    def get(self):
        """
        Handles the initial request to the app.
        Checks if the player is involved in a game. If he is, take him to the game
        else, create a channel between client and server
        """
        user = users.get_current_user()
        if not user:
            return self.redirect(users.create_login_url('/'))
        userid = user.user_id()
        player = Player(userid)
        if player.get_gameroom():    
            self.template_values.update({'gamekey': player.get_gameroom()})
        clientid_for_channel = player.get_channel()
        token = channel.create_channel(clientid_for_channel)
        self.template_values.update({'token' : token,
                            'userid' : player.userid})
        return self.render('index.html')


class JoinGame(BaseHandler):
    def post(self):
        """
        Processes the req from a client/player to join a gameroom
        """
        user = users.get_current_user()
        if not user:
            return self.redirect(users.create_login_url('/'))
        
        userid = user.user_id()
        logging.info('join chat call by %s'%userid)
        player = Player(userid)
        if not player.get_gameroom():
            gameroom = Tournament.join_latest_or_new(player)
            return
        Tournament.continue_tournament(player)


class Chat(webapp.RequestHandler):
    """ 
    Handles when players chat between each other
    """
    def post(self):
        user = users.get_current_user()
        if not user:
            return self.redirect(users.create_login_url('/'))
        userid = user.user_id()
        message = self.request.params.get('m')
        player = Player(userid)
        game_room = player.get_gameroom()
        t = Tournament(game_room)
        t.chat(player, message)

class Bye(BaseHandler):
    """
    Handles the Bye
    """
    def get(self):
        self.response.out.write("""<html><body>
                Bye. It was nice to see you here !!<a href="/">Login Again</a>
                </bosy></html>""")


application = webapp.WSGIApplication(
                            [('/', MainPage),
                             ('/joingame.*', JoinGame),
                             ('/chat', Chat),
                             ('/bye', Bye)],
                            debug=True)

def main():
    run_wsgi_app(application)


if __name__ == "__main__":
    main()
