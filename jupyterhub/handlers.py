"""HTTP Handlers for the hub server"""

# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.

import json
import re

import requests

from tornado.log import app_log
from tornado.escape import url_escape
from tornado.httputil import url_concat
from tornado.web import RequestHandler
from tornado import gen, web

from . import orm
from .spawner import LocalProcessSpawner
from .utils import wait_for_server, url_path_join


class BaseHandler(RequestHandler):
    """Base Handler class with access to common methods and properties."""
    
    @property
    def log(self):
        """I can't seem to avoid typing self.log"""
        return self.settings.get('log', app_log)
    
    @property
    def config(self):
        return self.settings.get('config', None)
    
    @property
    def base_url(self):
        return self.settings.get('base_url', '/')
    
    @property
    def db(self):
        return self.settings['db']
    
    @property
    def hub(self):
        return self.settings['hub']
    
    def get_current_user(self):
        if 'get_current_user' in self.settings:
            return self.settings['get_current_user'](self)
        
        token = self.get_cookie(self.hub.server.cookie_name, None)
        if token:
            cookie_token = self.db.query(orm.CookieToken).filter(orm.CookieToken.token==token).first()
            if cookie_token:
                return cookie_token.user.name
            else:
                # have cookie, but it's not valid. Clear it and start over.
                self.clear_cookie(self.hub.server.cookie_name)
    
    def clear_login_cookie(self):
        username = self.get_current_user()
        if username is not None:
            user = self.db.query(orm.User).filter(name=username).first()
            if user is not None:
                self.clear_cookie(user.server.cookie_name, path=user.server.base_url)
        self.clear_cookie(self.cookie_name, path=self.hub.base_url)
    
    @property
    def spawner_class(self):
        return self.settings.get('spawner_class', LocalProcessSpawner)


class RootHandler(BaseHandler):
    """Redirect from / to /user/foo/ after logging in."""
    @web.authenticated
    def get(self):
        self.redirect("/user/%s/" % self.get_current_user())


class UserHandler(BaseHandler):
    """Respawn single-user server after logging in.
    
    This handler shouldn't be called if the proxy is set up correctly.
    """
    @web.authenticated
    def get(self, user):
        self.log.warn("Hub caught serving single-user url: %s", user)
        if self.get_current_user() == user:
            self.spawn_single_user(user)
            self.redirect('')
        else:
            self.clear_login_cookie()
            self.redirect(url_concat(self.settings['login_url'], {
                'next' : self.request.path,
            }))


class LogoutHandler(BaseHandler):
    """Log a user out by clearing their login cookie."""
    def get(self):
        self.clear_login_cookie()
        self.write("logged out")

class LoginHandler(BaseHandler):
    """Render the login page."""

    def _render(self, message=None, username=None):
        self.render('login.html',
                next=url_escape(self.get_argument('next', default='')),
                username=username,
                message=message,
        )

    def get(self):
        if False and self.get_current_user():
            self.redirect(self.get_argument('next', default='/'))
        else:
            username = self.get_argument('username', default='')
            self._render(username=username)
    
    @gen.coroutine
    def notify_proxy(self, user):
        proxy = self.db.query(orm.Proxy).first()
        r = requests.post(
            url_path_join(
                proxy.api_server.url,
                user.server.base_url,
            ),
            data=json.dumps(dict(
                target=user.server.url,
                user=user.name,
            )),
            headers={'Authorization': "token %s" % proxy.auth_token},
        )
        yield wait_for_server(user.server.ip, user.server.port)
        r.raise_for_status()
    
    @gen.coroutine
    def spawn_single_user(self, name):
        user = orm.User(name=name,
            server=orm.Server(
                cookie_name='%s-%s' % (self.hub.server.cookie_name, name),
                cookie_secret=self.hub.server.cookie_secret,
                base_url=url_path_join(self.base_url, 'user', name),
            ),
        )
        self.db.add(user)
        self.db.commit()
        
        api_token = user.new_api_token()
        self.db.add(api_token)
        self.db.commit()
        
        spawner = user.spawner = self.spawner_class(
            config=self.config,
            user=user,
            hub=self.hub,
            api_token=api_token.token,
        )
        yield spawner.start()
        
        # store state
        user.state = spawner.get_state()
        self.db.commit()
        
        self.notify_proxy(user)
        raise gen.Return(user)
    
    def set_login_cookies(self, user):
        """Set login cookies for the Hub and single-user server."""
        # create and set a new cookie token for the single-user server
        cookie_token = user.new_cookie_token()
        self.db.add(cookie_token)
        self.db.commit()
        
        self.set_cookie(
            user.server.cookie_name,
            cookie_token.token,
            path=user.server.base_url,
        )
        
        # create and set a new cookie token for the hub
        cookie_token = user.new_cookie_token()
        self.db.add(cookie_token)
        self.db.commit()
        self.set_cookie(
            self.hub.server.cookie_name,
            cookie_token.token,
            path=self.hub.server.base_url)
    
    @gen.coroutine
    def authenticate(self, data):
        auth = self.settings.get('authenticator', None)
        if auth is not None:
            result = yield auth.authenticate(self, data)
            raise gen.Return(result)
        else:
            self.log.error("No authentication function, login is impossible!")
    
    @gen.coroutine
    def post(self):
        # parse the arguments dict
        data = {}
        for arg in self.request.arguments:
            data[arg] = self.get_argument(arg)
        
        username = data['username']
        authorized = yield self.authenticate(data)
        if authorized:
            user = self.db.query(orm.User).filter(orm.User.name == username).first()
            if user is None:
                user = yield self.spawn_single_user(username)
            self.set_login_cookies(user)
            next_url = self.get_argument('next', default='') or '/user/%s/' % username
            self.redirect(next_url)
        else:
            self.log.debug("Failed login for %s", username)
            self._render(
                message={'error': 'Invalid username or password'},
                username=username,
            )


#------------------------------------------------------------------------------
# API Handlers
#------------------------------------------------------------------------------

# pattern for the authentication token header
auth_header_pat = re.compile(r'^token\s+([^\s]+)$')

def token_authorized(method):
    """decorator for a method authorized by the Authorization token header"""
    def check_token(self, *args, **kwargs):
        auth_header = self.request.headers.get('Authorization', '')
        match = auth_header_pat.match(auth_header)
        if not match:
            raise web.HTTPError(403)
        token = match.group(1)
        db_token = self.db.query(orm.APIToken).filter(orm.APIToken.token == token).first()
        if db_token is None:
            raise web.HTTPError(403)
        return method(self, *args, **kwargs)
    check_token.__name__ = method.__name__
    check_token.__doc__ = method.__doc__
    return check_token


class AuthorizationsHandler(BaseHandler):
    @token_authorized
    def get(self, token):
        db_token = self.db.query(orm.CookieToken).filter(orm.CookieToken.token == token).first()
        if db_token is None:
            raise web.HTTPError(404)
        self.write(json.dumps({
            'user' : db_token.user.name,
        }))
