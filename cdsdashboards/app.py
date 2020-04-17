"""
Application for configuring and building the app environments.
"""

import os
import re
import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from traitlets import Unicode, Integer, Bool, Dict, validate, Any, Type, default, observe
from traitlets.config import Application, catch_config_error
from tornado.httpclient import AsyncHTTPClient
from tornado.httpserver import HTTPServer
import tornado.ioloop
import tornado.options
import tornado.log
from sqlalchemy.exc import OperationalError
from jinja2 import Environment, FileSystemLoader, PrefixLoader, ChoiceLoader
from jupyterhub.services.auth import HubOAuthCallbackHandler
from jupyterhub import __version__ as __jh_version__
from jupyterhub import dbutil

from .dashboard import DashboardRepr
from .util import url_path_join
from jupyterhub import orm as jhorm
from .builder.builders import BuildersDict, Builder
from .builder.dockerbuilder import DockerBuilder
from ._data import DATA_FILES_PATH

CDS_TEMPLATE_PATH = os.path.join(DATA_FILES_PATH, 'templates')

common_aliases = {
    'log-level': 'Application.log_level',
    'f': 'CDSDashboards.config_file',
    'config': 'CDSDashboards.config_file',
    'db': 'CDSDashboards.db_url',
}

class UpgradeDB(Application):
    """Upgrade the CDSDashboards database schema."""

    name = 'cdsdashboards-upgrade-db'
    version = __jh_version__
    description = """Upgrade the CDSDashboards database to the current schema.

    Usage:

        cdsdashboards upgrade-db
    """
    aliases = common_aliases
    classes = []

    def start(self):
        self.log.debug('Starting upgrade-db')

        hub = CDSDashboards(parent=self)
        hub.load_config_file(hub.config_file)
        self.log = hub.log

        self.log.debug('DB URL {}'.format(hub.db_url))

        # my_table_names = set(Base.metadata.tables.keys())

        dbutil.upgrade_if_needed(hub.db_url, log=self.log)

        self.log.debug('Finished upgrade-db')


class CDSDashboards(Application):
    """An Application for starting a builder."""

    subcommands = {
        'upgrade-db': (
            UpgradeDB,
            "Upgrade your CDSDashboards state database to the current version.",
        ),
    }

    @default('log_level')
    def _log_level(self):
        return logging.INFO

    debug = Bool(
        False,
        help="""
        Turn on debugging.
        """,
        config=True
    )

    use_registry = Bool(
        False,
        help="""
        Set to true to push images to a registry & check for images in registry.

        Set to false to use only local docker images. Useful when running
        in a single node.
        """,
        config=True
    )

    port = Integer(
        8585,
        help="""
        Port for the builder to listen on.
        """,
        config=True
    )

    config_file = Unicode(
        'cdsdashboards_config.py',
        help="""
        Config file to load.

        If a relative path is provided, it is taken relative to current directory
        """,
        config=True
    )

    concurrent_build_limit = Integer(
        32,
        config=True,
        help="""The number of concurrent builds to allow."""
    )
    executor_threads = Integer(
        5,
        config=True,
        help="""The number of threads to use for blocking calls

        Should generaly be a small number because we don't
        care about high concurrency here, just not blocking the webserver.
        This executor is not used for long-running tasks (e.g. builds).
        """,
    )

    template_path = Unicode(
        help="Path to search for custom jinja templates, before using the default templates.",
        config=True,
    )

    @default('template_path')
    def _template_path_default(self):
        return CDS_TEMPLATE_PATH

    tornado_settings = Dict(
        config=True,
        help="""
        additional settings to pass through to tornado.

        can include things like additional headers, etc.
        """
    )

    base_url = Unicode(
        os.environ.get('JUPYTERHUB_SERVICE_PREFIX', '/'),
        help="The base URL of the entire application",
        config=True)

    @validate('base_url')
    def _valid_base_url(self, proposal):
        if not proposal.value.startswith('/'):
            proposal.value = '/' + proposal.value
        if not proposal.value.endswith('/'):
            proposal.value = proposal.value + '/'
        return proposal.value

    extra_footer_scripts = Dict(
        {},
        help="""
        Extra bits of JavaScript that should be loaded in footer of each page.

        Only the values are set up as scripts. Keys are used only
        for sorting.

        Omit the <script> tag. This should be primarily used for
        analytics code.
        """,
        config=True
    )

    # TODO This builder_class config is not currently used - always just does DockerBuilder
    builder_class = Type(default_value=DockerBuilder, klass=Builder).tag(config=True)

    hub_api_token = Unicode(
        help="""API token for talking to the JupyterHub API""",
        config=True,
    )
    @default('hub_api_token')
    def _default_hub_token(self):
        return os.environ.get('JUPYTERHUB_API_TOKEN', '')

    hub_url = Unicode(
        help="""
        The base URL of the JupyterHub instance where users will run.

        e.g. https://hub.mybinder.org/
        """,
        config=True,
    )
    @validate('hub_url')
    def _add_slash(self, proposal):
        """trait validator to ensure hub_url ends with a trailing slash"""
        if proposal.value is not None and not proposal.value.endswith('/'):
            return proposal.value + '/'
        return proposal.value

    def init_pycurl(self):
        try:
            AsyncHTTPClient.configure("tornado.curl_httpclient.CurlAsyncHTTPClient")
        except ImportError as e:
            self.log.debug("Could not load pycurl: %s\npycurl is recommended if you have a large number of users.", e)
        # set max verbosity of curl_httpclient at INFO
        # because debug-logging from curl_httpclient
        # includes every full request and response
        if self.log_level < logging.INFO:
            curl_log = logging.getLogger('tornado.curl_httpclient')
            curl_log.setLevel(logging.INFO)

    db_url = Unicode(
        'sqlite:///jupyterhub.sqlite',
        help="url for the database. e.g. `sqlite:///jupyterhub.sqlite`",
    ).tag(config=True)

    @observe('db_url')
    def _db_url_changed(self, change):
        new = change['new']
        if '://' not in new:
            # assume sqlite, if given as a plain filename
            self.db_url = 'sqlite:///%s' % new

    db_kwargs = Dict(
        help="""Include any kwargs to pass to the database connection.
        See sqlalchemy.create_engine for details.
        """
    ).tag(config=True)

    upgrade_db = Bool(
        False,
        help="""Upgrade the database automatically on start.

        Only safe if database is regularly backed up.
        Only SQLite databases will be backed up to a local file automatically.
    """,
    ).tag(config=True)
    reset_db = Bool(False, help="Purge and reset the database.").tag(config=True)
    debug_db = Bool(
        False, help="log all database transactions. This has A LOT of output"
    ).tag(config=True)
    session_factory = Any()

    def init_db(self):
        """Create the database connection"""

        urlinfo = urlparse(self.db_url)
        if urlinfo.password:
            # avoid logging the database password
            urlinfo = urlinfo._replace(
                netloc='{}:[redacted]@{}:{}'.format(
                    urlinfo.username, urlinfo.hostname, urlinfo.port
                )
            )
            db_log_url = urlinfo.geturl()
        else:
            db_log_url = self.db_url
        self.log.debug("Connecting to db: %s", db_log_url)
        if self.upgrade_db:
            dbutil.upgrade_if_needed(self.db_url, log=self.log)

        try:
            self.session_factory = jhorm.new_session_factory(
                self.db_url, reset=self.reset_db, echo=self.debug_db, **self.db_kwargs
            )
            self.db = self.session_factory()
        except OperationalError as e:
            self.log.error("Failed to connect to db: %s", db_log_url)
            self.log.debug("Database error was:", exc_info=True)
            if self.db_url.startswith('sqlite:///'):
                self._check_db_path(self.db_url.split(':///', 1)[1])
            self.log.critical(
                '\n'.join(
                    [
                        "If you recently upgraded JupyterHub, try running",
                        "    jupyterhub upgrade-db",
                        "to upgrade your JupyterHub database schema",
                    ]
                )
            )
            self.exit(1)
        except jhorm.DatabaseSchemaMismatch as e:
            self.exit(e)

    def _check_db_path(self, *args, **kwargs):
        raise Exception('Not yet implemented')

    @catch_config_error
    def initialize(self, *args, **kwargs):
        """Load configuration settings."""
        super().initialize(*args, **kwargs)
        self.load_config_file(self.config_file)

        if self.subapp:
            return

        # hook up tornado logging
        if self.debug:
            self.log_level = logging.DEBUG
        tornado.options.options.logging = logging.getLevelName(self.log_level)
        tornado.log.enable_pretty_logging()
        self.log = tornado.log.app_log

        self.init_pycurl()
        self.init_db()

        # times 2 for log + build threads
        self.build_pool = ThreadPoolExecutor(self.concurrent_build_limit * 2)
        # default executor for asyncifying blocking calls (e.g. to kubernetes, docker).
        # this should not be used for long-running requests
        self.executor = ThreadPoolExecutor(self.executor_threads)

        jinja_options = dict(autoescape=True, )
        template_paths = [self.template_path]
        base_template_path = self._template_path_default()
        if base_template_path not in template_paths:
            # add base templates to the end, so they are looked up at last after custom templates
            template_paths.append(base_template_path)
        loader = ChoiceLoader([
            # first load base templates with prefix
            PrefixLoader({'templates': FileSystemLoader([base_template_path])}, '/'),
            # load all templates
            FileSystemLoader(template_paths)
        ])
        jinja_env = Environment(loader=loader, **jinja_options)
        if self.use_registry:
            #registry = DockerRegistry(parent=self)
            pass
        else:
            registry = None

        self.dashboard = DashboardRepr(
            parent=self,
            hub_url=self.hub_url,
            hub_api_token=self.hub_api_token
        )

        #self.event_log = EventLog(parent=self)

        #for schema_file in glob(os.path.join(HERE, 'event-schemas','*.json')):
        #    with open(schema_file) as f:
        #        self.event_log.register_schema(json.load(f))

        self.tornado_settings.update({
            "debug": self.debug,
            'base_url': self.base_url,
            "static_path": os.path.join(DATA_FILES_PATH, "static"),
            'static_url_prefix': url_path_join(self.base_url, 'static/'),
            #'template_variables': self.template_variables,
            #'executor': self.executor,
            'build_pool': self.build_pool,
            'use_registry': self.use_registry,
            'traitlets_config': self.config,
            'registry': registry,
            'jinja2_env': jinja_env,
            'extra_footer_scripts': self.extra_footer_scripts,
            'dashboard': self.dashboard,
            #'event_log': self.event_log,
            #'normalized_origin': self.normalized_origin
        })

        self.tornado_settings['cookie_secret'] = os.urandom(32)

        handlers = [
            #(r'/metrics', MetricsHandler),
            #(r'/versions', VersionHandler),
            #(r"/build/([^/]+)/(.+)", BuildHandler),
            #(r"/v2/([^/]+)/(.+)", ParameterizedMainHandler),
            #(r"/repo/([^/]+)/([^/]+)(/.*)?", LegacyRedirectHandler),
            ## for backward-compatible mybinder.org badge URLs
            ## /assets/images/badge.svg
            #(r'/assets/(images/badge\.svg)',
            # tornado.web.StaticFileHandler,
            # {'path': self.tornado_settings['static_path']}),
            ## /badge.svg
            #(r'/(badge\.svg)',
            # tornado.web.StaticFileHandler,
            # {'path': os.path.join(self.tornado_settings['static_path'], 'images')}),
            ## /badge_logo.svg
            #(r'/(badge\_logo\.svg)',
             #tornado.web.StaticFileHandler,
             #{'path': os.path.join(self.tornado_settings['static_path'], 'images')}),
            # /logo_social.png
            #(r'/(logo\_social\.png)',
             #tornado.web.StaticFileHandler,
             #{'path': os.path.join(self.tornado_settings['static_path'], 'images')}),
            # /favicon_XXX.ico
            #(r'/(favicon\_fail\.ico)',
            # tornado.web.StaticFileHandler,
            # {'path': os.path.join(self.tornado_settings['static_path'], 'images')}),
            #(r'/(favicon\_success\.ico)',
            # tornado.web.StaticFileHandler,
            # {'path': os.path.join(self.tornado_settings['static_path'], 'images')}),
            #(r'/(favicon\_building\.ico)',
            # tornado.web.StaticFileHandler,
            # {'path': os.path.join(self.tornado_settings['static_path'], 'images')}),
            #(r'/about', AboutHandler),
            #(r'/health', HealthHandler, {'hub_url': self.hub_url}),
            #(self.base_url + r'(?P<user_name>[^/]+)/app/(?P<server_name>[^/]+)?', MainDashboardHandler),
            #(r'.*', Custom404),
        ]
        #handlers = self.add_url_prefix(self.base_url, handlers)
        #if self.extra_static_path:
        #    handlers.insert(-1, (re.escape(url_path_join(self.base_url, self.extra_static_url_prefix)) + r"(.*)",
        #                         tornado.web.StaticFileHandler,
        #                         {'path': self.extra_static_path}))

        oauth_redirect_uri = os.getenv('JUPYTERHUB_OAUTH_CALLBACK_URL') or \
                             url_path_join(self.base_url, 'oauth_callback')
        oauth_redirect_uri = urlparse(oauth_redirect_uri).path
        handlers.insert(-1, (re.escape(oauth_redirect_uri), HubOAuthCallbackHandler))

        self.log.info(self.base_url)

        self.tornado_app = tornado.web.Application(handlers, **self.tornado_settings)


    def start(self):
        if self.subapp:
            self.subapp.start()
            return

        self.log.info("CDSDashboards starting on port %i", self.port)
        self.http_server = HTTPServer(
            self.tornado_app,
            xheaders=True,
        )
        self.http_server.listen(self.port)

        #if self.builder_required:
        #    asyncio.ensure_future(self.watch_build_pods())

        tornado.ioloop.IOLoop.current().start()

    def stop(self):
        self.http_server.stop()
        #self.build_pool.shutdown()


UpgradeDB.classes.append(CDSDashboards)

main = CDSDashboards.launch_instance



def builder_factory(dashboard):
    tornado.log.app_log.debug("Builder factory for key {}".format(dashboard.id))
    return DockerBuilder(dashboard=dashboard)


builders_store = BuildersDict(builder_factory)

cds_tornado_settings = {
    'cds_builders': builders_store,
}

if __name__ == '__main__':
    main()
