import base64
import copy
import json
import socket
import threading
import uuid

import sys
from collections import namedtuple

try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO

from pathlib import Path

from unittest import mock

import unittest

import rollbar
from rollbar.lib import string_types

from rollbar.test import BaseTest
from rollbar.test.utils import get_public_attrs

try:
    eval("""
        def _anonymous_tuple_func(x, (a, b), y):
            ret = x + a + b + y
            breakme()
            return ret
    """)
except SyntaxError:
    _anonymous_tuple_func = None


_test_access_token = 'aaaabbbbccccddddeeeeffff00001111'
_default_settings = copy.deepcopy(rollbar.SETTINGS)


class RollbarTest(BaseTest):
    def setUp(self):
        rollbar._initialized = False
        rollbar.SETTINGS = copy.deepcopy(_default_settings)
        rollbar.init(_test_access_token, locals={'enabled': True}, dummy_key='asdf', handler='blocking', timeout=12345)

    def test_merged_settings(self):
        expected = {'enabled': True, 'sizes': rollbar.DEFAULT_LOCALS_SIZES, 'safe_repr': True, 'scrub_varargs': True, 'safelisted_types': [], 'whitelisted_types': []}
        self.assertDictEqual(rollbar.SETTINGS['locals'], expected)
        self.assertEqual(rollbar.SETTINGS['timeout'], 12345)
        self.assertEqual(rollbar.SETTINGS['dummy_key'], 'asdf')

    def test_default_configuration(self):
        self.assertEqual(rollbar.SETTINGS['access_token'], _test_access_token)
        self.assertEqual(rollbar.SETTINGS['environment'], 'production')

    @mock.patch('rollbar.send_payload')
    def test_disabled(self, send_payload):
        rollbar.SETTINGS['enabled'] = False

        rollbar.report_message('foo')
        try:
            raise Exception('foo')
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, False)

    def test_server_data(self):
        server_data = rollbar._build_server_data()

        self.assertIn('host', server_data)
        self.assertIn('argv', server_data)
        self.assertNotIn('branch', server_data)
        self.assertNotIn('root', server_data)
        self.assertGreater(len(server_data['host']), 2)

        rollbar.SETTINGS['host'] = 'test-host'
        rollbar.SETTINGS['branch'] = 'master'
        rollbar.SETTINGS['root'] = '/home/test/'

        server_data = rollbar._build_server_data()

        self.assertIn('argv', server_data)
        self.assertEqual(server_data['host'], 'test-host')
        self.assertEqual(server_data['branch'], 'master')
        self.assertEqual(server_data['root'], '/home/test/')

    def test_wsgi_request_data(self):
        rollbar.SETTINGS['include_request_body'] = True
        request = {
            'CONTENT_LENGTH': str(len('body body body')),
            'CONTENT_TYPE': '',
            'DOCUMENT_URI': '/api/test',
            'GATEWAY_INTERFACE': 'CGI/1.1',
            'HTTP_CONNECTION': 'close',
            'HTTP_HOST': 'example.com',
            'HTTP_USER_AGENT': 'Agent',
            'PATH_INFO': '/api/test',
            'QUERY_STRING': 'format=json&param1=value1&param2=value2',
            'REMOTE_ADDR': '127.0.0.1',
            'REQUEST_METHOD': 'GET',
            'SCRIPT_NAME': '',
            'SERVER_ADDR': '127.0.0.1',
            'SERVER_NAME': 'example.com',
            'SERVER_PORT': '80',
            'SERVER_PROTOCOL': 'HTTP/1.1',
            'wsgi.input': StringIO('body body body'),
            'wsgi.multiprocess': True,
            'wsgi.multithread': False,
            'wsgi.run_once': False,
            'wsgi.url_scheme': 'http',
            'wsgi.version': (1, 0)
        }
        data = rollbar._build_wsgi_request_data(request)
        self.assertEqual(data['url'], 'http://example.com/api/test?format=json&param1=value1&param2=value2')
        self.assertEqual(data['user_ip'], '127.0.0.1')
        self.assertEqual(data['method'], 'GET')
        self.assertEqual(data['body'], 'body body body')
        self.assertDictEqual(data['GET'], {'format': 'json', 'param1': 'value1', 'param2': 'value2'})
        self.assertDictEqual(data['headers'], {'Connection': 'close', 'Host': 'example.com', 'User-Agent': 'Agent'})

    def test_wsgi_request_data_no_body(self):
        rollbar.SETTINGS['include_request_body'] = False
        request = {
            'CONTENT_LENGTH': str(len('body body body')),
            'REMOTE_ADDR': '127.0.0.1',
            'SERVER_NAME': 'example.com',
            'SERVER_PORT': '80',
            'wsgi.input': StringIO('body body body'),
            'wsgi.url_scheme': 'http',
        }
        data = rollbar._build_wsgi_request_data(request)
        self.assertNotIn('body', data)
        rollbar.SETTINGS['include_request_body'] = True

    def test_starlette_request_data(self):
        try:
            from starlette.requests import Request
        except ImportError:
            self.skipTest('Requires Starlette to be installed')

        scope = {
            'type': 'http',
            'client': ('127.0.0.1', 1453),
            'headers': [
                (b'accept', b'*/*'),
                (b'content-type', b'application/x-www-form-urlencoded'),
                (b'host', b'example.com'),
                (b'user-agent', b'Agent'),
            ],
            'http_version': '1.1',
            'method': 'GET',
            'path': '/api/test',
            'path_params': {'param': 'test'},
            'query_params': {
                'format': 'json',
                'param1': 'value1',
                'param2': 'value2',
            },
            'query_string': b'format=json&param1=value1&param2=value2',
            'scheme': 'http',
            'server': ('example.com', 80),
            'url': {'path': 'example.com'},
        }
        request = Request(scope)
        data = rollbar._build_starlette_request_data(request)

        self.assertEqual(data['url'], 'http://example.com/api/test?format=json&param1=value1&param2=value2')
        self.assertEqual(data['user_ip'], '127.0.0.1')
        self.assertEqual(data['method'], 'GET')
        self.assertDictEqual(data['params'], {'param': 'test'})
        self.assertDictEqual(data['GET'], {'format': 'json', 'param1': 'value1', 'param2': 'value2'})
        self.assertDictEqual(
            data['headers'],
            {
                'accept': '*/*',
                'content-type': 'application/x-www-form-urlencoded',
                'host': 'example.com',
                'user-agent': 'Agent',
            },
        )

    def test_starlette_request_data_with_consumed_body(self):
        try:
            from starlette.requests import Request
        except ImportError:
            self.skipTest('Requires Starlette to be installed')
        from rollbar.lib._async import async_receive, run

        rollbar.SETTINGS['include_request_body'] = True
        body = b'body body body'
        scope = {
            'type': 'http',
            'client': ('127.0.0.1', 1453),
            'headers': [
                (b'content-type', b'text/html'),
                (b'content-length', str(len(body)).encode('latin-1')),
            ],
            'method': 'GET',
            'path': '/api/test',
            'query_string': b'',
        }
        receive = async_receive(
            {'type': 'http.request', 'body': body, 'mode_body': False}
        )
        request = Request(scope, receive)

        # Consuming body in Starlette middleware is currently disabled
        run(request.body()) # await request.body()

        data = rollbar._build_starlette_request_data(request)

        self.assertEqual(data['body'], body.decode('latin-1'))

    def test_starlette_request_data_empty_values(self):
        try:
            from starlette.requests import Request
        except ImportError:
            self.skipTest('Requires Starlette to be installed')

        scope = {
            'type': 'http',
            'client': ('127.0.0.1', 1453),
            'headers': [
                (b'content-type', b'text/html'),
            ],
            'method': 'GET',
            'query_string': b'',
            'path': '',
        }
        request = Request(scope)

        data = rollbar._build_starlette_request_data(request)

        self.assertFalse('GET' in data)
        self.assertFalse('url' in data)
        self.assertFalse('params' in data)
        self.assertTrue('headers' in data)
        self.assertEqual(data['user_ip'], scope['client'][0])
        self.assertEqual(data['method'], scope['method'])

    def test_fastapi_request_data(self):
        try:
            from fastapi.requests import Request
        except ImportError:
            self.skipTest('Requires FastAPI to be installed')

        scope = {
            'type': 'http',
            'client': ('127.0.0.1', 1453),
            'headers': [
                (b'accept', b'*/*'),
                (b'content-type', b'application/x-www-form-urlencoded'),
                (b'host', b'example.com'),
                (b'user-agent', b'Agent'),
            ],
            'http_version': '1.1',
            'method': 'GET',
            'path': '/api/test',
            'path_params': {'param': 'test'},
            'query_params': {
                'format': 'json',
                'param1': 'value1',
                'param2': 'value2',
            },
            'query_string': b'format=json&param1=value1&param2=value2',
            'scheme': 'http',
            'server': ('example.com', 80),
            'url': {'path': 'example.com'},
        }
        request = Request(scope)
        data = rollbar._build_fastapi_request_data(request)

        self.assertEqual(data['url'], 'http://example.com/api/test?format=json&param1=value1&param2=value2')
        self.assertEqual(data['user_ip'], '127.0.0.1')
        self.assertEqual(data['method'], 'GET')
        self.assertDictEqual(data['params'], {'param': 'test'})
        self.assertDictEqual(data['GET'], {'format': 'json', 'param1': 'value1', 'param2': 'value2'})
        self.assertDictEqual(
            data['headers'],
            {
                'accept': '*/*',
                'content-type': 'application/x-www-form-urlencoded',
                'host': 'example.com',
                'user-agent': 'Agent',
            },
        )

    def test_fastapi_request_data_with_consumed_body(self):
        try:
            from fastapi import Request
        except ImportError:
            self.skipTest('Requires FastAPI to be installed')
        from rollbar.lib._async import async_receive, run

        rollbar.SETTINGS['include_request_body'] = True
        body = b'body body body'
        scope = {
            'type': 'http',
            'headers': [
                (b'content-type', b'text/html'),
                (b'content-length', str(len(body)).encode('latin-1')),
            ],
            'method': 'GET',
            'path': '/api/test',
            'query_string': b'',
        }
        receive = async_receive(
            {'type': 'http.request', 'body': body, 'mode_body': False}
        )
        request = Request(scope, receive)

        # Consuming body in FastAPI middlewares is currently disabled
        run(request.body()) # await request.body()

        data = rollbar._build_fastapi_request_data(request)

        self.assertEqual(data['body'], body.decode('latin-1'))

    def test_fastapi_request_data_empty_values(self):
        try:
            from fastapi import Request
        except ImportError:
            self.skipTest('Requires FastAPI to be installed')

        scope = {
            'type': 'http',
            'client': ('127.0.0.1', 1453),
            'headers': [
                (b'content-type', b'text/html'),
            ],
            'method': 'GET',
            'query_string': b'',
            'path': '',
        }
        request = Request(scope)

        data = rollbar._build_fastapi_request_data(request)

        self.assertFalse('GET' in data)
        self.assertFalse('url' in data)
        self.assertFalse('params' in data)
        self.assertTrue('headers' in data)
        self.assertEqual(data['user_ip'], scope['client'][0])
        self.assertEqual(data['method'], scope['method'])

    def test_django_build_person_data(self):
        try:
            import django
            from django.conf import settings
        except ImportError:
            self.skipTest('Requires Django to be installed')
        else:
            settings.configure(
                INSTALLED_APPS=['django.contrib.auth', 'django.contrib.contenttypes']
            )
            if django.VERSION >= (1, 7):
                django.setup()

        from django.contrib.auth.models import User
        from django.http.request import HttpRequest

        request = HttpRequest()
        request.user = User()
        request.user.id = 123
        request.user.username = 'admin'
        request.user.email = 'admin@example.org'

        data = rollbar._build_person_data(request)

        self.assertDictEqual(
            data, {'id': '123', 'username': 'admin', 'email': 'admin@example.org'}
        )

    def test_starlette_build_person_data_if_user_authenticated(self):
        try:
            from starlette.authentication import SimpleUser
            from starlette.requests import Request
        except ImportError:
            self.skipTest('Requires Starlette to be installed')

        # Implement interface with the id attribute
        class User(SimpleUser):
            counter = 0

            def __init__(self, username, email):
                super().__init__(username)
                self.email = email

                User.counter += 1
                self.id = User.counter

        scope = {'type': 'http'}
        request = Request(scope)
        # Make the user authenticated
        request.scope['user'] = User('admin', 'admin@example.org')

        data = rollbar._build_person_data(request)

        self.assertDictEqual(
            data, {'id': '1', 'username': 'admin', 'email': 'admin@example.org'}
        )

    def test_starlette_failsafe_build_person_data_if_user_not_authenticated(self):
        try:
            from starlette.requests import Request
        except ImportError:
            self.skipTest('Requires Starlette to be installed')

        scope = {'type': 'http'}
        request = Request(scope)

        data = rollbar._build_person_data(request)

        self.assertIsNone(data)

    @unittest.skipUnless(sys.version_info >= (3, 6), 'Python3.6+ required')
    def test_get_request_starlette_middleware(self):
        try:
            from starlette.applications import Starlette
            from starlette.middleware import Middleware
            from starlette.responses import PlainTextResponse
            from starlette.routing import Route
            from starlette.testclient import TestClient
        except ImportError:
            self.skipTest('Requires Starlette package')
        from rollbar.contrib.starlette import ReporterMiddleware

        def root(starlette_request):
            current_request = rollbar.get_request()

            self.assertEqual(get_public_attrs(current_request), get_public_attrs(starlette_request))

            return PlainTextResponse("bye bye")

        routes = [Route('/{param}', root)]
        middleware = [Middleware(ReporterMiddleware)]
        app = Starlette(routes=routes, middleware=middleware)
        client = TestClient(app)
        response = client.get('/test?param1=value1&param2=value2')

        self.assertEqual(response.status_code, 200)

    @unittest.skipUnless(sys.version_info >= (3, 6), 'Python3.6+ required')
    def test_get_request_starlette_logger(self):
        try:
            from starlette.applications import Starlette
            from starlette.middleware import Middleware
            from starlette.responses import PlainTextResponse
            from starlette.routing import Route
            from starlette.testclient import TestClient
        except ImportError:
            self.skipTest('Requires Starlette package')
        from rollbar.contrib.starlette import ReporterMiddleware

        def root(starlette_request):
            current_request = rollbar.get_request()

            self.assertEqual(get_public_attrs(current_request), get_public_attrs(starlette_request))

            return PlainTextResponse("bye bye")

        routes = [Route('/{param}', root)]
        middleware = [Middleware(ReporterMiddleware)]
        app = Starlette(routes=routes, middleware=middleware)
        client = TestClient(app)
        response = client.get('/test?param1=value1&param2=value2')

        self.assertEqual(response.status_code, 200)

    @unittest.skipUnless(sys.version_info >= (3, 6), 'Python3.6+ required')
    def test_get_request_fastapi_middleware(self):
        try:
            from fastapi import FastAPI, Request
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest('Requires FastaAPI package')
        from rollbar.contrib.fastapi import ReporterMiddleware

        app = FastAPI()
        app.add_middleware(ReporterMiddleware)

        @app.get('/{param}')
        def root(param, fastapi_request: Request):
            current_request = rollbar.get_request()

            self.assertEqual(get_public_attrs(current_request), get_public_attrs(fastapi_request))

        root = fastapi_add_route_with_request_param(
            app, root, '/{param}', 'fastapi_request'
        )

        client = TestClient(app)
        response = client.get('/test?param1=value1&param2=value2')

        self.assertEqual(response.status_code, 200)

    @unittest.skipUnless(sys.version_info >= (3, 6), 'Python3.6+ required')
    def test_get_request_fastapi_logger(self):
        try:
            from fastapi import FastAPI, Request
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest('Requires FastaAPI package')
        from rollbar.contrib.fastapi import ReporterMiddleware

        app = FastAPI()
        app.add_middleware(ReporterMiddleware)

        @app.get('/{param}')
        def root(fastapi_request: Request):
            current_request = rollbar.get_request()

            self.assertEqual(get_public_attrs(current_request), get_public_attrs(fastapi_request))

        root = fastapi_add_route_with_request_param(
            app, root, '/{param}', 'fastapi_request'
        )

        client = TestClient(app)
        response = client.get('/test?param1=value1&param2=value2')

        self.assertEqual(response.status_code, 200)

    @unittest.skipUnless(sys.version_info >= (3, 6), 'Python3.6+ required')
    def test_get_request_fastapi_router(self):
        try:
            import fastapi
            from fastapi import FastAPI, Request
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest('Requires FastAPI package')
        from rollbar.contrib.fastapi import add_to as rollbar_add_to

        if fastapi.__version__ < '0.41.0':
            self.skipTest('Requires FastAPI 0.41.0+')

        app = FastAPI()
        rollbar_add_to(app)

        @app.get('/{param}')
        def root(fastapi_request: Request):
            current_request = rollbar.get_request()

            self.assertEqual(get_public_attrs(current_request), get_public_attrs(fastapi_request))

        root = fastapi_add_route_with_request_param(
            app, root, '/{param}', 'fastapi_request'
        )

        client = TestClient(app)
        response = client.get('/test?param1=value1&param2=value2')

        self.assertEqual(response.status_code, 200)

    @mock.patch('rollbar.send_payload')
    def test_report_exception(self, send_payload):

        def _raise():
            try:
                raise Exception('foo')
            except:
                rollbar.report_exc_info()

        _raise()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertEqual(payload['access_token'], _test_access_token)
        self.assertIn('body', payload['data'])
        self.assertIn('trace', payload['data']['body'])
        self.assertNotIn('trace_chain', payload['data']['body'])
        self.assertIn('exception', payload['data']['body']['trace'])
        self.assertEqual(payload['data']['body']['trace']['exception']['message'], 'foo')
        self.assertEqual(payload['data']['body']['trace']['exception']['class'], 'Exception')

        self.assertNotIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('keywordspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('locals', payload['data']['body']['trace']['frames'][-1])

    @mock.patch('rollbar._post_api')
    def test_lambda_function_good(self, _post_api):
        rollbar.SETTINGS['handler'] = 'thread'
        fake_event = {'a': 42}
        fake_context = MockLambdaContext(99)
        @rollbar.lambda_function
        def my_lambda_func(event, context):
            return [event['a'], context.x]

        result = my_lambda_func(fake_event, fake_context)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], 42)
        self.assertEqual(result[1], 99)
        self.assertEqual(_post_api.called, False)

        rollbar._CURRENT_LAMBDA_CONTEXT = None
        rollbar.SETTINGS['handler'] = 'blocking'

    @mock.patch('rollbar._post_api')
    def test_lambda_function_bad(self, _post_api):
        rollbar.SETTINGS['handler'] = 'thread'
        fake_event = {'a': 42}
        fake_context = MockLambdaContext(99)
        @rollbar.lambda_function
        def my_lambda_func(event, context):
            raise event['a']

        result = None
        try:
            result = my_lambda_func(fake_event, fake_context)
        except:
            pass

        self.assertEqual(result, None)
        self.assertEqual(_post_api.called, True)

        rollbar._CURRENT_LAMBDA_CONTEXT = None
        rollbar.SETTINGS['handler'] = 'blocking'

    @mock.patch('rollbar._post_api')
    def test_lambda_function_method_good(self, _post_api):
        rollbar.SETTINGS['handler'] = 'thread'
        fake_event = {'a': 42}
        fake_context = MockLambdaContext(99)

        class LambdaClass(object):
            def __init__(self):
                self.a = 13

            def my_lambda_func(self, event, context):
                return [event['a'], context.x, self.a]

        app = LambdaClass()
        app.my_lambda_func = rollbar.lambda_function(app.my_lambda_func)
        result = app.my_lambda_func(fake_event, fake_context)

        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], 42)
        self.assertEqual(result[1], 99)
        self.assertEqual(result[2], 13)
        self.assertEqual(_post_api.called, False)

        rollbar._CURRENT_LAMBDA_CONTEXT = None
        rollbar.SETTINGS['handler'] = 'blocking'

    @mock.patch('rollbar._post_api')
    def test_lambda_function_method_bad(self, _post_api):
        rollbar.SETTINGS['handler'] = 'thread'
        fake_event = {'a': 42}
        fake_context = MockLambdaContext(99)

        class LambdaClass(object):
            def __init__(self):
                self.a = 13

            def my_lambda_func(self, event, context):
                raise self.a

        app = LambdaClass()
        app.my_lambda_func = rollbar.lambda_function(app.my_lambda_func)

        result = None
        try:
            result = app.my_lambda_func(fake_event, fake_context)
        except:
            pass

        self.assertEqual(result, None)
        self.assertEqual(_post_api.called, True)

        rollbar._CURRENT_LAMBDA_CONTEXT = None
        rollbar.SETTINGS['handler'] = 'blocking'

    @mock.patch('rollbar.send_payload')
    def test_report_exception_with_cause(self, send_payload):
        def _raise_cause():
            bar_local = 'bar'
            raise CauseException('bar')

        def _raise_ex():
            try:
                _raise_cause()
            except CauseException as cause:
                # python2 won't automatically assign this traceback...
                exc_info = sys.exc_info()
                setattr(cause, '__traceback__', exc_info[2])

                try:
                    foo_local = 'foo'
                    # in python3 this would normally be expressed as
                    # raise Exception('foo') from cause
                    e = Exception('foo')
                    setattr(e, '__cause__', cause)  # PEP-3134
                    raise e
                except:
                    rollbar.report_exc_info()

        _raise_ex()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertEqual(payload['access_token'], _test_access_token)
        self.assertIn('body', payload['data'])
        self.assertNotIn('trace', payload['data']['body'])
        self.assertIn('trace_chain', payload['data']['body'])
        self.assertEqual(2, len(payload['data']['body']['trace_chain']))

        self.assertIn('exception', payload['data']['body']['trace_chain'][0])
        self.assertEqual(payload['data']['body']['trace_chain'][0]['exception']['message'], 'foo')
        self.assertEqual(payload['data']['body']['trace_chain'][0]['exception']['class'], 'Exception')
        self.assertEqual(payload['data']['body']['trace_chain'][0]['frames'][-1]['locals']['foo_local'], 'foo')

        self.assertIn('exception', payload['data']['body']['trace_chain'][1])
        self.assertEqual(payload['data']['body']['trace_chain'][1]['exception']['message'], 'bar')
        self.assertEqual(payload['data']['body']['trace_chain'][1]['exception']['class'], 'CauseException')
        self.assertEqual(payload['data']['body']['trace_chain'][1]['frames'][-1]['locals']['bar_local'], 'bar')

    @mock.patch('rollbar.send_payload')
    def test_report_exception_with_same_exception_as_cause(self, send_payload):
        cause_exc = CauseException('bar')

        def _raise_cause():
            bar_local = 'bar'
            raise cause_exc

        def _raise_ex():
            try:
                _raise_cause()
            except CauseException as cause:
                # python2 won't automatically assign this traceback...
                exc_info = sys.exc_info()
                setattr(cause, '__traceback__', exc_info[2])

                try:
                    foo_local = 'foo'
                    # in python3 this would normally be expressed as
                    # raise cause from cause
                    setattr(cause, '__cause__', cause)  # PEP-3134
                    raise cause
                except:
                    rollbar.report_exc_info()

        ex_raiser = threading.Thread(target=_raise_ex)
        ex_raiser.daemon = True
        ex_raiser.start()
        # 0.5 seconds ought be enough for any modern computer to get into the
        # cyclical parts of the code, but not so long as to collect a lot of
        # objects in memory
        ex_raiser.join(timeout=0.5)

        if ex_raiser.is_alive():
            # This breaks the circular reference, allowing thread to exit and
            # to be joined
            cause_exc.__cause__ = None
            ex_raiser.join()
            self.fail('Cyclic reference in rollbar._walk_trace_chain()')

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertEqual(payload['access_token'], _test_access_token)
        self.assertIn('body', payload['data'])
        self.assertNotIn('trace', payload['data']['body'])
        self.assertIn('trace_chain', payload['data']['body'])
        self.assertEqual(2, len(payload['data']['body']['trace_chain']))

        self.assertIn('exception', payload['data']['body']['trace_chain'][0])
        self.assertEqual(payload['data']['body']['trace_chain'][0]['exception']['message'], 'bar')
        self.assertEqual(payload['data']['body']['trace_chain'][0]['exception']['class'], 'CauseException')
        frames = payload['data']['body']['trace_chain'][0]['frames']
        self.assertEqual(payload['data']['body']['trace_chain'][0]['frames'][0]['locals']['foo_local'], 'foo')

        self.assertIn('exception', payload['data']['body']['trace_chain'][1])
        self.assertEqual(payload['data']['body']['trace_chain'][1]['exception']['message'], 'bar')
        self.assertEqual(payload['data']['body']['trace_chain'][1]['exception']['class'], 'CauseException')
        self.assertEqual(payload['data']['body']['trace_chain'][1]['frames'][-1]['locals']['bar_local'], 'bar')

    @mock.patch('rollbar.send_payload')
    def test_report_exception_with_context(self, send_payload):
        def _raise_context():
            bar_local = 'bar'
            raise CauseException('bar')

        def _raise_ex():
            try:
                _raise_context()
            except CauseException as context:
                # python2 won't automatically assign this traceback...
                exc_info = sys.exc_info()
                setattr(context, '__traceback__', exc_info[2])

                try:
                    foo_local = 'foo'
                    # in python3 __context__ is automatically set when an exception is raised in an except block
                    e = Exception('foo')
                    setattr(e, '__context__', context)  # PEP-3134
                    raise e
                except:
                    rollbar.report_exc_info()

        _raise_ex()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertEqual(payload['access_token'], _test_access_token)
        self.assertIn('body', payload['data'])
        self.assertNotIn('trace', payload['data']['body'])
        self.assertIn('trace_chain', payload['data']['body'])
        self.assertEqual(2, len(payload['data']['body']['trace_chain']))

        self.assertIn('exception', payload['data']['body']['trace_chain'][0])
        self.assertEqual(payload['data']['body']['trace_chain'][0]['exception']['message'], 'foo')
        self.assertEqual(payload['data']['body']['trace_chain'][0]['exception']['class'], 'Exception')
        self.assertEqual(payload['data']['body']['trace_chain'][0]['frames'][-1]['locals']['foo_local'], 'foo')

        self.assertIn('exception', payload['data']['body']['trace_chain'][1])
        self.assertEqual(payload['data']['body']['trace_chain'][1]['exception']['message'], 'bar')
        self.assertEqual(payload['data']['body']['trace_chain'][1]['exception']['class'], 'CauseException')
        self.assertEqual(payload['data']['body']['trace_chain'][1]['frames'][-1]['locals']['bar_local'], 'bar')

    @mock.patch('rollbar.send_payload')
    def test_exception_filters(self, send_payload):

        rollbar.SETTINGS['exception_level_filters'] = [
            (OSError, 'ignored'),
            ('rollbar.ApiException', 'ignored'),
            ('bogus.DoesntExist', 'ignored'),
        ]

        def _raise_exception():
            try:
                raise Exception('foo')
            except:
                rollbar.report_exc_info()

        def _raise_os_error():
            try:
                raise OSError('bar')
            except:
                rollbar.report_exc_info()

        def _raise_api_exception():
            try:
                raise rollbar.ApiException('bar')
            except:
                rollbar.report_exc_info()

        _raise_exception()
        self.assertTrue(send_payload.called)

        _raise_os_error()
        self.assertEqual(1, send_payload.call_count)

        _raise_api_exception()
        self.assertEqual(1, send_payload.call_count)

    @mock.patch('rollbar.send_payload')
    def test_report_messsage(self, send_payload):
        rollbar.report_message('foo')

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertEqual(payload['access_token'], _test_access_token)
        self.assertIn('body', payload['data'])
        self.assertIn('message', payload['data']['body'])
        self.assertIn('body', payload['data']['body']['message'])
        self.assertEqual(payload['data']['body']['message']['body'], 'foo')

    @mock.patch('rollbar.send_payload')
    def test_uuid(self, send_payload):
        uuid = rollbar.report_message('foo')

        payload = send_payload.call_args[0][0]

        self.assertEqual(payload['data']['uuid'], uuid)

    @mock.patch('rollbar.send_payload')
    def test_report_exc_info_level(self, send_payload):

        try:
            raise Exception('level_error')
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)
        payload = send_payload.call_args[0][0]
        self.assertEqual(payload['data']['level'], 'error')

        try:
            raise Exception('level_info')
        except:
            rollbar.report_exc_info(level='info')

        self.assertEqual(send_payload.called, True)
        payload = send_payload.call_args[0][0]
        self.assertEqual(payload['data']['level'], 'info')

        # payload takes precendence over 'level'
        try:
            raise Exception('payload_warn')
        except:
            rollbar.report_exc_info(level='info', payload_data={'level': 'warn'})

        self.assertEqual(send_payload.called, True)
        payload = send_payload.call_args[0][0]
        self.assertEqual(payload['data']['level'], 'warn')

    @mock.patch('rollbar.send_payload')
    def test_report_exc_info_nones(self, send_payload):

        rollbar.report_exc_info(exc_info=(None, None, None))

        self.assertEqual(send_payload.called, True)
        payload = send_payload.call_args[0][0]
        self.assertEqual(payload['data']['level'], 'error')

    @mock.patch('rollbar._send_failsafe')
    @mock.patch('rollbar.lib.transport.post',
                side_effect=lambda *args, **kw: MockResponse({'status': 'Payload Too Large'}, 413))
    def test_trigger_failsafe(self, post, _send_failsafe):
        rollbar.report_message('derp')
        self.assertEqual(_send_failsafe.call_count, 1)

        try:
            raise Exception('trigger_failsafe')
        except:
            rollbar.report_exc_info()
            self.assertEqual(_send_failsafe.call_count, 2)

    @mock.patch('rollbar._send_failsafe')
    @mock.patch('rollbar.lib.transport.post',
                side_effect=lambda *args, **kw: MockRawResponse('<html>\r\n' \
                                                             '<head><title>502 Bad Gateway</title></head>\r\n' \
                                                             '<body bgcolor="white">\r\n' \
                                                             '<center><h1>502 Bad Gateway</h1></center>\r\n' \
                                                             '<hr><center>nginx</center>\r\n' \
                                                             '</body>\r\n' \
                                                             '</html>\r\n', 502))
    def test_502_failsafe(self, post, _send_failsafe):
        rollbar.report_message('derp')
        # self.assertEqual(_send_failsafe.call_count, 1)

        try:
            raise Exception('trigger_failsafe')
        except:
            rollbar._post_api('/api/1/item', {'derp'})

    @mock.patch('rollbar.send_payload')
    def test_send_failsafe(self, send_payload):
        test_uuid = str(uuid.uuid4())
        test_host = socket.gethostname()
        test_data = {
            'access_token': _test_access_token,
            'data': {
                'body': {
                    'message': {
                        'body': 'Failsafe from pyrollbar: test message. '
                                'Original payload may be found in your server '
                                'logs by searching for the UUID.'
                    }
                },
                'failsafe': True,
                'level': 'error',
                'custom': {
                    'orig_host': test_host,
                    'orig_uuid': test_uuid
                },
                'environment': rollbar.SETTINGS['environment'],
                'internal': True,
                'notifier': rollbar.SETTINGS['notifier']
            }
        }

        rollbar._send_failsafe('test message', test_uuid, test_host)
        self.assertEqual(send_payload.call_count, 1)
        self.assertEqual(send_payload.call_args[0][0], test_data)

    @mock.patch('rollbar.log.exception')
    @mock.patch('rollbar.send_payload', side_effect=Exception('Monkey Business!'))
    def test_fail_to_send_failsafe(self, send_payload, mock_log):
        test_uuid = str(uuid.uuid4())
        test_host = socket.gethostname()
        rollbar._send_failsafe('test message', test_uuid, test_host)
        self.assertEqual(mock_log.call_count, 1)

    @unittest.skipUnless(rollbar.AsyncHTTPClient, 'Requires async handler to be installed')
    @mock.patch('rollbar._send_payload_async')
    def test_async_handler(self, send_payload_async):
        def _raise():
            try:
                raise Exception('foo')
            except:
                rollbar.report_exc_info()

        rollbar.SETTINGS['handler'] = 'async'
        _raise()

        send_payload_async.assert_called_once()

    @unittest.skipUnless(rollbar.httpx, 'Requires HTTPX to be installed')
    @mock.patch('rollbar._send_payload_httpx')
    def test_httpx_handler(self, send_payload_httpx):
        def _raise():
            try:
                raise Exception('foo')
            except:
                rollbar.report_exc_info()

        rollbar.SETTINGS['handler'] = 'async'
        _raise()

        send_payload_httpx.assert_called_once()

    @unittest.skipUnless(sys.version_info >= (3, 6), 'assert_called_once support requires Python3.6+')
    @mock.patch('rollbar._send_payload_thread_pool')
    def test_thread_pool_handler(self, send_payload_thread_pool):
        def _raise():
            try:
                raise Exception('foo')
            except:
                rollbar.report_exc_info()
        rollbar.SETTINGS['handler'] = 'thread_pool'
        _raise()

        send_payload_thread_pool.assert_called_once()

    @unittest.skipUnless(sys.version_info >= (3, 2), 'concurrent.futures support requires Python3.2+')
    def test_thread_pool_submit(self):
        from rollbar.lib.thread_pool import init_pool, submit
        init_pool(1)
        ran = {'nope': True}  # dict used so it is not shadowed in run

        def run(payload_str, access_token):
            ran['nope'] = False

        submit(run, 'foo', 'bar')
        self.assertFalse(ran['nope'])


    @mock.patch('rollbar.send_payload')
    def test_args_constructor(self, send_payload):

        class tmp(object):
            def __init__(self, arg1):
                self.arg1 = arg1
                foo()

        try:
            t = tmp(33)
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('keywordspec', payload['data']['body']['trace']['frames'][-1])

        self.assertEqual('arg1', payload['data']['body']['trace']['frames'][-1]['argspec'][1])
        self.assertEqual(33, payload['data']['body']['trace']['frames'][-1]['locals']['arg1'])

    @mock.patch('rollbar.send_payload')
    def test_failed_locals_serialization(self, send_payload):

        class tmp(object):
            @property
            def __class__(self):
                foo()

        try:
            t = tmp()
            raise Exception('trigger_serialize')
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

    @mock.patch('rollbar.send_payload')
    def test_args_lambda_no_args(self, send_payload):

        _raise = lambda: foo()

        try:
            _raise()
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertNotIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('keywordspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('locals', payload['data']['body']['trace']['frames'][-1])

    @mock.patch('rollbar.send_payload')
    def test_args_lambda_with_args(self, send_payload):

        _raise = lambda arg1, arg2: foo(arg1, arg2)

        try:
            _raise('arg1-value', 'arg2-value')
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('keywordspec', payload['data']['body']['trace']['frames'][-1])

        self.assertEqual(2, len(payload['data']['body']['trace']['frames'][-1]['argspec']))
        self.assertEqual('arg1', payload['data']['body']['trace']['frames'][-1]['argspec'][0])
        self.assertEqual('arg1-value', payload['data']['body']['trace']['frames'][-1]['locals']['arg1'])
        self.assertEqual('arg2', payload['data']['body']['trace']['frames'][-1]['argspec'][1])
        self.assertEqual('arg2-value', payload['data']['body']['trace']['frames'][-1]['locals']['arg2'])

    @mock.patch('rollbar.send_payload')
    def test_args_lambda_with_defaults(self, send_payload):

        _raise = lambda arg1='default': foo(arg1)

        try:
            _raise(arg1='arg1-value')
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('keywordspec', payload['data']['body']['trace']['frames'][-1])

        # NOTE(cory): Lambdas are a bit strange. We treat default values for lambda args
        #             as positional.
        self.assertEqual(1, len(payload['data']['body']['trace']['frames'][-1]['argspec']))
        self.assertEqual('arg1', payload['data']['body']['trace']['frames'][-1]['argspec'][0])
        self.assertEqual('arg1-value', payload['data']['body']['trace']['frames'][-1]['locals']['arg1'])

    @mock.patch('rollbar.send_payload')
    def test_args_lambda_with_star_args(self, send_payload):

        _raise = lambda *args: foo(arg1)

        try:
            _raise('arg1-value')
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertNotIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('keywordspec', payload['data']['body']['trace']['frames'][-1])

        varargs = payload['data']['body']['trace']['frames'][-1]['varargspec']

        self.assertEqual(1, len(payload['data']['body']['trace']['frames'][-1]['locals'][varargs]))
        self.assertRegex(payload['data']['body']['trace']['frames'][-1]['locals'][varargs][0], r'\*+')

    @mock.patch('rollbar.send_payload')
    def test_args_lambda_with_star_args_and_args(self, send_payload):

        _raise = lambda arg1, *args: foo(arg1)

        try:
            _raise('arg1-value', 1, 2)
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('keywordspec', payload['data']['body']['trace']['frames'][-1])

        varargs = payload['data']['body']['trace']['frames'][-1]['varargspec']

        self.assertEqual(1, len(payload['data']['body']['trace']['frames'][-1]['argspec']))
        self.assertEqual('arg1', payload['data']['body']['trace']['frames'][-1]['argspec'][0])
        self.assertEqual('arg1-value', payload['data']['body']['trace']['frames'][-1]['locals']['arg1'])

        self.assertEqual(2, len(payload['data']['body']['trace']['frames'][-1]['locals'][varargs]))
        self.assertRegex(payload['data']['body']['trace']['frames'][-1]['locals'][varargs][0], r'\*+')
        self.assertRegex(payload['data']['body']['trace']['frames'][-1]['locals'][varargs][1], r'\*+')

    @mock.patch('rollbar.send_payload')
    def test_args_lambda_with_kwargs(self, send_payload):

        _raise = lambda **kwargs: foo(arg1)

        try:
            _raise(arg1='arg1-value', arg2=2)
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertNotIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertIn('keywordspec', payload['data']['body']['trace']['frames'][-1])

        keywords = payload['data']['body']['trace']['frames'][-1]['keywordspec']

        self.assertEqual(2, len(payload['data']['body']['trace']['frames'][-1]['locals'][keywords]))
        self.assertEqual('arg1-value', payload['data']['body']['trace']['frames'][-1]['locals'][keywords]['arg1'])
        self.assertEqual(2, payload['data']['body']['trace']['frames'][-1]['locals'][keywords]['arg2'])

    @mock.patch('rollbar.send_payload')
    def test_args_lambda_with_kwargs_and_args(self, send_payload):

        _raise = lambda arg1, arg2, **kwargs: foo(arg1)

        try:
            _raise('a1', 'a2', arg3='arg3-value', arg4=2)
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertIn('keywordspec', payload['data']['body']['trace']['frames'][-1])

        keywords = payload['data']['body']['trace']['frames'][-1]['keywordspec']

        self.assertEqual(2, len(payload['data']['body']['trace']['frames'][-1]['argspec']))
        self.assertEqual('arg1', payload['data']['body']['trace']['frames'][-1]['argspec'][0])
        self.assertEqual('arg2', payload['data']['body']['trace']['frames'][-1]['argspec'][1])
        self.assertEqual('a1', payload['data']['body']['trace']['frames'][-1]['locals']['arg1'])
        self.assertEqual('a2', payload['data']['body']['trace']['frames'][-1]['locals']['arg2'])

        self.assertEqual(2, len(payload['data']['body']['trace']['frames'][-1]['locals'][keywords]))
        self.assertEqual('arg3-value', payload['data']['body']['trace']['frames'][-1]['locals'][keywords]['arg3'])
        self.assertEqual(2, payload['data']['body']['trace']['frames'][-1]['locals'][keywords]['arg4'])

    @mock.patch('rollbar.send_payload')
    def test_args_lambda_with_kwargs_and_args_and_defaults(self, send_payload):

        _raise = lambda arg1, arg2, arg3='default-value', **kwargs: foo(arg1)

        try:
            _raise('a1', 'a2', arg3='arg3-value', arg4=2)
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertIn('keywordspec', payload['data']['body']['trace']['frames'][-1])

        keywords = payload['data']['body']['trace']['frames'][-1]['keywordspec']

        # NOTE(cory): again, default values are strange for lambdas and we include them as
        #             positional args.
        self.assertEqual(3, len(payload['data']['body']['trace']['frames'][-1]['argspec']))
        self.assertEqual('arg1', payload['data']['body']['trace']['frames'][-1]['argspec'][0])
        self.assertEqual('arg2', payload['data']['body']['trace']['frames'][-1]['argspec'][1])
        self.assertEqual('arg3', payload['data']['body']['trace']['frames'][-1]['argspec'][2])
        self.assertEqual('a1', payload['data']['body']['trace']['frames'][-1]['locals']['arg1'])
        self.assertEqual('a2', payload['data']['body']['trace']['frames'][-1]['locals']['arg2'])
        self.assertEqual('arg3-value', payload['data']['body']['trace']['frames'][-1]['locals']['arg3'])

        self.assertEqual(1, len(payload['data']['body']['trace']['frames'][-1]['locals'][keywords]))
        self.assertEqual(2, payload['data']['body']['trace']['frames'][-1]['locals'][keywords]['arg4'])

    @mock.patch('rollbar.send_payload')
    def test_args_generators(self, send_payload):

        def _raise(arg1):
            for i in range(2):
                if i > 0:
                    raise Exception()
                else:
                    yield i

        try:
            l = list(_raise('hello world'))
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('keywordspec', payload['data']['body']['trace']['frames'][-1])

        self.assertEqual(1, len(payload['data']['body']['trace']['frames'][-1]['argspec']))
        self.assertEqual('arg1', payload['data']['body']['trace']['frames'][-1]['argspec'][0])
        self.assertEqual('hello world', payload['data']['body']['trace']['frames'][-1]['locals']['arg1'])

    @mock.patch('rollbar.send_payload')
    def test_anonymous_tuple_args(self, send_payload):

        # Only run this test on Python versions that support it
        if not _anonymous_tuple_func:
            return

        try:
            _anonymous_tuple_func((1, (2, 3), 4))
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('keywordspec', payload['data']['body']['trace']['frames'][-1])

        self.assertEqual(4, len(payload['data']['body']['trace']['frames'][-1]['argspec']))
        self.assertEqual(1, payload['data']['body']['trace']['frames'][-1]['argspec'][0])
        self.assertEqual(2, payload['data']['body']['trace']['frames'][-1]['argspec'][1])
        self.assertEqual(3, payload['data']['body']['trace']['frames'][-1]['argspec'][2])
        self.assertEqual(4, payload['data']['body']['trace']['frames'][-1]['argspec'][3])
        self.assertEqual(10, payload['data']['body']['trace']['frames'][-1]['locals']['ret'])

    @mock.patch('rollbar.send_payload')
    def test_scrub_defaults(self, send_payload):

        def _raise(password='sensitive', clear='text'):
            headers = { 'Authorization': 'bearer 123' }

            raise Exception()

        try:
            _raise()
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('kwargs', payload['data']['body']['trace']['frames'][-1]['locals'])

        self.assertEqual(2, len(payload['data']['body']['trace']['frames'][-1]['argspec']))
        self.assertEqual('password', payload['data']['body']['trace']['frames'][-1]['argspec'][0])
        self.assertRegex(payload['data']['body']['trace']['frames'][-1]['locals']['password'], r'\*+')
        self.assertRegex(payload['data']['body']['trace']['frames'][-1]['locals']['headers']['Authorization'], r'\*+')
        self.assertEqual('clear', payload['data']['body']['trace']['frames'][-1]['argspec'][1])
        self.assertEqual('text', payload['data']['body']['trace']['frames'][-1]['locals']['clear'])

    @mock.patch('rollbar.send_payload')
    def test_dont_scrub_star_args(self, send_payload):
        rollbar.SETTINGS['locals']['scrub_varargs'] = False

        def _raise(*args):
            raise Exception()

        try:
            _raise('sensitive', 'text')
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertNotIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('keywordspec', payload['data']['body']['trace']['frames'][-1])
        self.assertIn('locals', payload['data']['body']['trace']['frames'][-1])

        varargspec = payload['data']['body']['trace']['frames'][-1]['varargspec']

        self.assertEqual(2, len(payload['data']['body']['trace']['frames'][-1]['locals'][varargspec]))
        self.assertEqual(payload['data']['body']['trace']['frames'][-1]['locals'][varargspec][0], 'sensitive')
        self.assertEqual(payload['data']['body']['trace']['frames'][-1]['locals'][varargspec][1], 'text')

    @mock.patch('rollbar.send_payload')
    def test_scrub_kwargs(self, send_payload):

        def _raise(**kwargs):
            raise Exception()

        try:
            _raise(password='sensitive', clear='text')
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertNotIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertIn('keywordspec', payload['data']['body']['trace']['frames'][-1])

        keywords = payload['data']['body']['trace']['frames'][-1]['keywordspec']

        self.assertEqual(2, len(payload['data']['body']['trace']['frames'][-1]['locals'][keywords]))
        self.assertIn('password', payload['data']['body']['trace']['frames'][-1]['locals'][keywords])
        self.assertRegex(payload['data']['body']['trace']['frames'][-1]['locals'][keywords]['password'], r'\*+')
        self.assertIn('clear', payload['data']['body']['trace']['frames'][-1]['locals'][keywords])
        self.assertEqual('text', payload['data']['body']['trace']['frames'][-1]['locals'][keywords]['clear'])

    @mock.patch('rollbar.send_payload')
    def test_scrub_locals(self, send_payload):
        invalid_b64 = b'CuX2JKuXuLVtJ6l1s7DeeQ=='
        invalid = base64.b64decode(invalid_b64)

        def _raise():
            # Make sure that the _invalid local variable makes its
            # way into the payload even if its value cannot be serialized
            # properly.
            _invalid = invalid

            # Make sure the Password field gets scrubbed even though its
            # original value could not be serialized properly.
            Password = invalid

            password = 'sensitive'
            raise Exception((_invalid, Password, password))

        try:
            _raise()
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertRegex(payload['data']['body']['trace']['frames'][-1]['locals']['password'], r'\*+')
        self.assertRegex(payload['data']['body']['trace']['frames'][-1]['locals']['Password'], r'\*+')
        self.assertIn('_invalid', payload['data']['body']['trace']['frames'][-1]['locals'])

        undecodable_message = '<Undecodable type:(%s) base64:(%s)>' % ('bytes', base64.b64encode(invalid).decode('ascii'))
        self.assertEqual(undecodable_message, payload['data']['body']['trace']['frames'][-1]['locals']['_invalid'])

    @mock.patch('rollbar.send_payload')
    def test_scrub_namedtuple(self, send_payload):

        SomeTuple = namedtuple('SomeTuple', ['password', 'some_field'])

        def _raise():
            Data = SomeTuple(password='clear_text', some_field='some_field')

            password = 'sensitive'
            raise Exception((Data, password))

        try:
            _raise()
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertRegex(payload['data']['body']['trace']['frames'][-1]['locals']['password'], r'\*+')
        self.assertRegex(payload['data']['body']['trace']['frames'][-1]['locals']['Data'], 'password=\'\*+\'')

    @mock.patch('rollbar.send_payload')
    def test_scrub_nans(self, send_payload):
        def _raise():
            infinity = float('Inf')
            negative_infinity = float('-Inf')
            not_a_number = float('NaN')
            raise Exception()

        try:
            _raise()
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertEqual('<Infinity>', payload['data']['body']['trace']['frames'][-1]['locals']['infinity'])
        self.assertEqual('<NegativeInfinity>', payload['data']['body']['trace']['frames'][-1]['locals']['negative_infinity'])
        self.assertEqual('<NaN>', payload['data']['body']['trace']['frames'][-1]['locals']['not_a_number'])

    @mock.patch('rollbar.send_payload')
    def test_scrub_self_referencing(self, send_payload):
        def _raise(obj):
            raise Exception()

        try:
            obj = {'x': 42.3}
            obj['child'] = {
                'parent': obj
            }

            # NOTE(cory): We copy the dict here so that we don't produce a circular reference
            # from the _rase() args.
            _raise(dict(obj))
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertTrue(
            (isinstance(payload['data']['body']['trace']['frames'][-1]['locals']['obj'], dict) and
             'child' in payload['data']['body']['trace']['frames'][-1]['locals']['obj'])

             or

            (isinstance(payload['data']['body']['trace']['frames'][-1]['locals']['obj'], string_types) and
             payload['data']['body']['trace']['frames'][-1]['locals']['obj'].startswith('<CircularReference'))
        )

        self.assertTrue(
            (isinstance(payload['data']['body']['trace']['frames'][-1]['locals']['obj'], dict) and
             'x' in payload['data']['body']['trace']['frames'][-1]['locals']['obj'] and
             payload['data']['body']['trace']['frames'][-1]['locals']['obj']['x'] == 42.3)

             or

            (isinstance(payload['data']['body']['trace']['frames'][-1]['locals']['obj'], string_types) and
             payload['data']['body']['trace']['frames'][-1]['locals']['obj'].startswith('<CircularReference'))
        )

    @mock.patch('rollbar.send_payload')
    def test_scrub_local_ref(self, send_payload):
        """
        NOTE(cory): This test checks to make sure that we do not scrub a local variable that is a reference
                    to a parameter that is scrubbed.
                    Ideally we would be able to scrub 'copy' as well since we know that it has the same
                    value as a field that was scrubbed.
        """
        def _raise(password='sensitive'):
            copy = password
            raise Exception()

        try:
            _raise()
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertEqual('sensitive', payload['data']['body']['trace']['frames'][-1]['locals']['copy'])

    @mock.patch('rollbar.send_payload')
    def test_large_arg_val(self, send_payload):

        def _raise(large):
            raise Exception()

        try:
            large = ''.join(['#'] * 200)
            _raise(large)
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('keywordspec', payload['data']['body']['trace']['frames'][-1])

        self.assertEqual(1, len(payload['data']['body']['trace']['frames'][-1]['argspec']))
        self.assertEqual('large', payload['data']['body']['trace']['frames'][-1]['argspec'][0])
        self.assertEqual("################################################...#################################################",
                         payload['data']['body']['trace']['frames'][-1]['locals']['large'])

    @mock.patch('rollbar.send_payload')
    def test_long_list_arg_val(self, send_payload):

        def _raise(large):
            raise Exception()

        try:
            xlarge = ['hi' for _ in range(30)]
            # NOTE(cory): We copy the list here so that the local variables from
            # this frame are not referenced directly by the frame from _raise()
            # call above. If we didn't copy this list, Rollbar would report a
            # circular reference for the args on _raise().
            _raise([str(x) for x in xlarge])
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('keywordspec', payload['data']['body']['trace']['frames'][-1])

        self.assertEqual(1, len(payload['data']['body']['trace']['frames'][-1]['argspec']))

        self.assertEqual('large', payload['data']['body']['trace']['frames'][-1]['argspec'][0])
        self.assertTrue(
            (['hi', 'hi', 'hi', 'hi', 'hi', 'hi', 'hi', 'hi', 'hi', 'hi', '...'] ==
                payload['data']['body']['trace']['frames'][-1]['argspec'][0])

            or

            (['hi', 'hi', 'hi', 'hi', 'hi', 'hi', 'hi', 'hi', 'hi', 'hi', '...'] ==
                    payload['data']['body']['trace']['frames'][0]['locals']['xlarge']))


    @mock.patch('rollbar.send_payload')
    def test_last_frame_has_locals(self, send_payload):

        def _raise():
            some_var = 'some value'
            raise Exception()

        try:
            _raise()
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        self.assertNotIn('argspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('varargspec', payload['data']['body']['trace']['frames'][-1])
        self.assertNotIn('keywordspec', payload['data']['body']['trace']['frames'][-1])

        self.assertIn('locals', payload['data']['body']['trace']['frames'][-1])
        self.assertIn('some_var', payload['data']['body']['trace']['frames'][-1]['locals'])
        self.assertEqual("some value",
                         payload['data']['body']['trace']['frames'][-1]['locals']['some_var'])


    @mock.patch('rollbar.send_payload')
    def test_all_project_frames_have_locals(self, send_payload):

        prev_root = rollbar.SETTINGS['root']
        rollbar.SETTINGS['root'] = __file__.rstrip('pyc')
        try:
            step1()
        except:
            rollbar.report_exc_info()
        finally:
            rollbar.SETTINGS['root'] = prev_root

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]
        for frame in payload['data']['body']['trace']['frames']:
            self.assertIn('locals', frame)


    @mock.patch('rollbar.send_payload')
    def test_only_last_frame_has_locals(self, send_payload):

        prev_root = rollbar.SETTINGS['root']
        rollbar.SETTINGS['root'] = 'dummy'
        try:
            step1()
        except:
            rollbar.report_exc_info()
        finally:
            rollbar.SETTINGS['root'] = prev_root

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        num_frames = len(payload['data']['body']['trace']['frames'])
        for i, frame in enumerate(payload['data']['body']['trace']['frames']):
            if i < num_frames - 1:
                self.assertNotIn('locals', frame)
            else:
                self.assertIn('locals', frame)


    @mock.patch('rollbar.send_payload')
    def test_modify_arg(self, send_payload):
        # Record locals for all frames
        prev_root = rollbar.SETTINGS['root']
        rollbar.SETTINGS['root'] = __file__.rstrip('pyc')
        try:
            called_with('original value')
        except:
            rollbar.report_exc_info()
        finally:
            rollbar.SETTINGS['root'] = prev_root

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]

        frames = payload['data']['body']['trace']['frames']
        called_with_frame = frames[1]

        self.assertEqual('arg1', called_with_frame['argspec'][0])
        self.assertEqual('changed', called_with_frame['locals']['arg1'])

    @mock.patch('rollbar.send_payload')
    def test_unicode_exc_info(self, send_payload):
        message = '\u221a'

        try:
            raise Exception(message)
        except:
            rollbar.report_exc_info()

        self.assertEqual(send_payload.called, True)
        payload = send_payload.call_args[0][0]
        self.assertEqual(payload['data']['body']['trace']['exception']['message'], message)

    @mock.patch('rollbar.lib.transport.post', side_effect=lambda *args, **kw: MockResponse({'status': 'OK'}, 200))
    def test_serialize_and_send_payload(self, post=None):
        invalid_b64 = b'CuX2JKuXuLVtJ6l1s7DeeQ=='
        invalid = base64.b64decode(invalid_b64)

        def _raise():
            # Make sure that the _invalid local variable makes its
            # way into the payload even if its value cannot be serialized
            # properly.
            _invalid = invalid

            # Make sure the Password field gets scrubbed even though its
            # original value could not be serialized properly.
            Password = invalid

            password = 'sensitive'
            raise Exception('bug bug')

        try:
            _raise()
        except:
            rollbar.report_exc_info()

        self.assertEqual(post.called, True)
        payload_data = post.call_args[1]['data']
        self.assertIsInstance(payload_data, str)
        self.assertIn('bug bug', payload_data)

        try:
            post.call_args[1]['data']
        except:
            self.assertTrue(False)

    def test_scrub_webob_request_data(self):
        rollbar._initialized = False
        rollbar.init(_test_access_token, locals={'enabled': True}, dummy_key='asdf', handler='blocking', timeout=12345,
            scrub_fields=rollbar.SETTINGS['scrub_fields'] + ['token', 'secret', 'cookies', 'authorization'])

        import webob
        request = webob.Request.blank('/the/path?q=hello&password=hunter2',
                                      base_url='http://example.com',
                                      headers={
                                          'X-Real-Ip': '5.6.7.8',
                                          'Cookies': 'name=value; password=hash;',
                                          'Authorization': 'I am from NSA'
                                      },
                                      POST='foo=bar&confirm_password=hunter3&token=secret')

        unscrubbed = rollbar._build_webob_request_data(request)
        self.assertEqual(unscrubbed['url'], 'http://example.com/the/path?q=hello&password=hunter2')
        self.assertEqual(unscrubbed['user_ip'], '5.6.7.8')
        self.assertDictEqual(unscrubbed['GET'], {'q': 'hello', 'password': 'hunter2'})
        self.assertDictEqual(unscrubbed['POST'], {'foo': 'bar', 'confirm_password': 'hunter3', 'token': 'secret'})
        self.assertEqual('5.6.7.8', unscrubbed['headers']['X-Real-Ip'])
        self.assertEqual('name=value; password=hash;', unscrubbed['headers']['Cookies'])
        self.assertEqual('I am from NSA', unscrubbed['headers']['Authorization'])

        scrubbed = rollbar._transform(unscrubbed)
        self.assertRegex(scrubbed['url'], r'http://example.com/the/path\?(q=hello&password=-+)|(password=-+&q=hello)')

        self.assertEqual(scrubbed['GET']['q'], 'hello')
        self.assertRegex(scrubbed['GET']['password'], r'\*+')

        self.assertEqual(scrubbed['POST']['foo'], 'bar')
        self.assertRegex(scrubbed['POST']['confirm_password'], r'\*+')
        self.assertRegex(scrubbed['POST']['token'], r'\*+')

        self.assertEqual('5.6.7.8', scrubbed['headers']['X-Real-Ip'])

        self.assertRegex(scrubbed['headers']['Cookies'], r'\*+')
        self.assertRegex(scrubbed['headers']['Authorization'], r'\*+')

    def test_filter_ip_no_user_ip(self):
        request_data = {'something': 'but no ip'}
        rollbar._filter_ip(request_data, False)
        self.assertNotIn('user_ip', request_data)

    def test_filter_ip_capture_true(self):
        ip = '123.32.394.99'
        request_data = {'user_ip': ip}
        rollbar._filter_ip(request_data, True)
        self.assertEqual(ip, request_data['user_ip'])

    def test_filter_ip_anonymize(self):
        ip = '123.32.394.99'
        request_data = {'user_ip': ip}
        rollbar._filter_ip(request_data, rollbar.ANONYMIZE)
        self.assertNotEqual(ip, request_data['user_ip'])
        self.assertNotEqual(None, request_data['user_ip'])

    def test_filter_ip_capture_false(self):
        ip = '123.32.394.99'
        request_data = {'user_ip': ip}
        rollbar._filter_ip(request_data, False)
        self.assertNotEqual(ip, request_data['user_ip'])
        self.assertEqual(None, request_data['user_ip'])

    def test_filter_ip_ipv6_capture_false(self):
        ip = '2607:f0d0:1002:51::4'
        request_data = {'user_ip': ip}
        rollbar._filter_ip(request_data, False)
        self.assertNotEqual(ip, request_data['user_ip'])
        self.assertEqual(None, request_data['user_ip'])

    def test_filter_ip_anonymize_ipv6(self):
        ips = [
            'FE80:0000:0000:0000:0202:B3FF:FE1E:8329',
            'FE80::0202:B3FF:FE1E:8329',
            '2607:f0d0:1002:51::4',
        ]
        for ip in ips:
            request_data = {'user_ip': ip}
            rollbar._filter_ip(request_data, rollbar.ANONYMIZE)
            self.assertNotEqual(ip, request_data['user_ip'])
            self.assertNotEqual(None, request_data['user_ip'])

    def test_starlette_extract_user_ip_from_client_host(self):
        try:
            from starlette.requests import Request
        except ImportError:
            self.skipTest('Requires Starlette package')

        client_host = ('127.0.0.1', 1453)
        ip_forwarded_for = b'192.168.10.10'
        ip_real_ip = b'1.2.3.4'
        scope = {
            'type': 'http',
            'client': client_host,
            'headers': [
                (b'x-forwarded-for', ip_forwarded_for),
                (b'x-real-ip', ip_real_ip),
            ],
        }
        request = Request(scope)

        user_ip = rollbar._starlette_extract_user_ip(request)

        self.assertEqual(user_ip, client_host[0])

    def test_starlette_extract_user_ip_from_headers(self):
        try:
            from starlette.requests import Request
        except ImportError:
            self.skipTest('Requires Starlette package')

        ip_forwarded_for = b'192.168.10.10'
        ip_real_ip = b'1.2.3.4'

        # Headers contain only X-Forwarded-For
        scope = {'type': 'http', 'headers': [(b'x-forwarded-for', ip_forwarded_for)]}
        request = Request(scope)
        user_ip = rollbar._starlette_extract_user_ip(request)
        self.assertEqual(user_ip, ip_forwarded_for.decode())

        # Headers contain only X-Real-Ip
        scope = {'type': 'http', 'headers': [(b'x-real-ip', ip_real_ip)]}
        request = Request(scope)
        user_ip = rollbar._starlette_extract_user_ip(request)
        self.assertEqual(user_ip, ip_real_ip.decode())

        # Headers contain both X-Forwarded-For and X-Real-Ip
        scope = {
            'type': 'http',
            'headers': [
                (b'x-forwarded-for', ip_forwarded_for),
                (b'x-real-ip', ip_real_ip),
            ],
        }
        request = Request(scope)
        user_ip = rollbar._starlette_extract_user_ip(request)
        self.assertEqual(user_ip, ip_forwarded_for.decode())
    
    @mock.patch('rollbar.send_payload')
    def test_root_path(self, send_payload):
        prev_root = rollbar.SETTINGS['root']
        rollbar.SETTINGS['root'] = Path("/tmp")
        try:
            called_with('original value')
        except:
            rollbar.report_exc_info()
        finally:
            rollbar.SETTINGS['root'] = prev_root

        self.assertEqual(send_payload.called, True)

        payload = send_payload.call_args[0][0]
        self.assertEqual(payload['data']['server']['root'], "/tmp")


### Helpers

def step1():
    val1 = 1
    step2()


def step2():
    val2 = 2
    raise Exception()


def called_with(arg1):
    arg1 = 'changed'
    step1()


class CauseException(Exception):
    pass


class MockResponse:
    def __init__(self, json_data, status_code):
        self.json_data = json_data
        self.status_code = status_code

    @property
    def content(self):
        return json.dumps(self.json_data)

    def json(self):
        return self.json_data


class MockRawResponse:
    def __init__(self, data, status_code):
        self.data = data
        self.status_code = status_code

    @property
    def content(self):
        return self.data

    def json(self):
        return self.data


class MockLambdaContext(object):
    def __init__(self, x):
        self.function_name = 1
        self.function_version = 2
        self.invoked_function_arn = 3
        self.aws_request_id = 4
        self.x = x

    def get_remaining_time_in_millis(self):
        42


def fastapi_add_route_with_request_param(app, endpoint, path, request_param):
    from fastapi import Request

    endpoint.__annotations__[request_param] = Request

    return app.get(path)(endpoint)

if __name__ == '__main__':
    unittest.main()
