# -*- coding: utf-8; -*-
#
# The MIT License (MIT)
#
# Copyright (c) 2014 Flavien Charlon
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import argparse
import aiohttp
import aiohttp.server
import asyncio
import bitcoin.core
import configparser
import colorcore.caching
import colorcore.operations
import inspect
import json
import openassets.transactions
import re
import sys
import urllib.parse


class Program(object):
    """Main entry point of Colorcore."""

    @staticmethod
    def execute():
        configuration = Configuration()
        router = Router(
            colorcore.operations.Controller,
            sys.stdout,
            lambda: colorcore.caching.SqliteCache(configuration.cache_path),
            configuration,
            asyncio.new_event_loop(),
            "Colorcore: The Open Assets client for colored coins")
        router.parse(sys.argv[1:])


class Configuration():
    """Class for managing the Colorcore configuration file."""

    def __init__(self):
        parser = configparser.ConfigParser()
        config_path = 'config.ini'
        parser.read(config_path)

        self.rpc_url = parser['bitcoind']['rpcurl']
        self.version_byte = int(parser['environment']['version-byte'])
        self.p2sh_version_byte = int(parser['environment']['p2sh-version-byte'])
        self.dust_limit = int(parser['environment']['dust-limit'])
        self.default_fees = int(parser['environment']['default-fees'])
        self.cache_path = parser['cache']['path']

        if 'rpc' in parser:
            self.rpc_port = int(parser['rpc']['port'])
            self.rpc_enabled = True
        else:
            self.rpc_enabled = False


class RpcServer(aiohttp.server.ServerHttpProtocol):
    """The HTTP handler used to respond to JSON/RPC requests."""

    def __init__(self, controller, configuration, event_loop, cache_factory, **kwargs):
        super(RpcServer, self).__init__(loop=event_loop, **kwargs)
        self.controller = controller
        self.configuration = configuration
        self.cache_factory = cache_factory
        self.event_loop = event_loop

    @asyncio.coroutine
    def handle_request(self, message, payload):
        try:
            url = re.search('^/(?P<operation>\w+)$', message.path)
            if url is None:
                yield from self.error(102, 'The request path is invalid', message)
                return

            # Get the operation function corresponding to the URL path
            operation_name = url.group('operation')
            operation = getattr(self.controller, operation_name, None)

            if operation_name == '' or operation_name[0] == '_' or operation is None:
                yield from self.error(103, 'The operation name {name} is invalid'.format(name=operation_name), message)
                return

            # Read the POST body
            post_data = yield from payload.read()
            post_vars = urllib.parse.parse_qs(post_data)

            tx_parser = Router.get_transaction_formatter(post_vars.pop('txformat', 'json'))

            controller = self.controller(self.configuration, self.cache_factory, tx_parser, self.event_loop)

            try:
                result = yield from operation(controller, **post_vars)
            except TypeError:
                yield from self.error(104, 'Invalid parameters provided', message)
                return
            except ControllerError as error:
                yield from self.error(201, str(error), message)
                return
            except openassets.transactions.TransactionBuilderError as error:
                yield from self.error(301, type(error).__name__, message)
                return

            response = self.create_response(200, message)
            yield from self.json_response(response, result)

            if response.keep_alive():
                self.keep_alive(True)

        except Exception as exception:
            response = self.create_response(500, message)
            yield from self.json_response(
                response, {'error': {'code': 0, 'message': 'Internal server error', 'details': str(exception)}})

    def create_response(self, status, message):
        response = aiohttp.Response(self.writer, status, http_version=message.version)
        response.add_header('Content-Type', 'text/json')
        response.send_headers()
        return response

    @asyncio.coroutine
    def error(self, code, error, message):
        response = self.create_response(400, message)
        yield from self.json_response(response, {'error': {'code': code, 'message': error}})

    @asyncio.coroutine
    def json_response(self, response, data):
        response.write(bytes(json.dumps(data, indent=4, separators=(',', ': ')), 'utf-8'))
        yield from response.write_eof()


class Router:
    """Infrastructure for routing command line calls to the right function."""

    extra_parameters = [
        ('txformat', "Format of transactions if a transaction is returned ('raw' or 'json')", 'json')
    ]

    def __init__(self, controller, output, cache_factory, configuration, event_loop, description=None):
        self.controller = controller
        self.configuration = configuration
        self.event_loop = event_loop
        self.output = output
        self.cache_factory = cache_factory
        self._parser = argparse.ArgumentParser(description=description)
        subparsers = self._parser.add_subparsers()

        subparser = subparsers.add_parser('server', help="Starts the Colorcore JSON/RPC server.")
        subparser.set_defaults(_func=self._run_rpc_server)

        for name, function in inspect.getmembers(self.controller, predicate=inspect.isfunction):
            # Skip non-public functions
            if name[0] != '_':
                subparser = subparsers.add_parser(name, help=function.__doc__)
                self._create_subparser(subparser, configuration, function)

    def _create_subparser(self, subparser, configuration, func):
        subparser.set_defaults(_func=self._execute_operation(configuration, func))
        func_signature = inspect.signature(func)
        for name, arg in func_signature.parameters.items():
            if name == 'self':
                continue
            if arg.kind != arg.POSITIONAL_OR_KEYWORD:
                continue

            arg_help = arg.annotation if arg.annotation is not arg.empty else None
            if arg.default is arg.empty:
                # a positional argument
                subparser.add_argument(name, help=arg_help)
            else:
                # an optional argument
                subparser.add_argument('--' + name, help=arg_help, nargs='?', default=arg.default)

        for name, help, default in self.extra_parameters:
            subparser.add_argument('--' + name, help=help, nargs='?', default=default)

    def _execute_operation(self, configuration, function):
        def decorator(*args, txformat, **kwargs):
            # Instantiate the controller
            controller = self.controller(
                configuration, self.cache_factory, self.get_transaction_formatter(txformat), self.event_loop)

            @asyncio.coroutine
            def coroutine_wrapper():
                try:
                    # Execute the operation on the controller
                    result = yield from function(controller, *args, **kwargs)

                    # Write the output of the operation onto the output stream
                    self.output.write(json.dumps(result, indent=4, separators=(',', ': '), sort_keys=False) + '\n')

                except ControllerError as error:
                    # The controller raised a known error
                    self.output.write("Error: {}\n".format(str(error)))
                except openassets.transactions.TransactionBuilderError as error:
                    # A transaction could not be built
                    self.output.write("Error: {}\n".format(type(error).__name__))

            self.event_loop.run_until_complete(coroutine_wrapper())

        return decorator

    @staticmethod
    def get_transaction_formatter(format):
        """
        Returns a function for formatting output.

        :param str format: Either 'json' for returning a JSON representation of the transaction, or 'raw' to return the
            hex-encoded raw transaction. If the object is not a transaction, it is returned unmodified.
        :return: The formatted response.
        """
        if format == 'json':
            def get_transaction_json(transaction):
                if isinstance(transaction, bitcoin.core.CTransaction):
                    return {
                        'version': transaction.nVersion,
                        'locktime': transaction.nLockTime,
                        'vin': [{
                                'txid': bitcoin.core.b2lx(input.prevout.hash),
                                'vout': input.prevout.n,
                                'sequence': input.nSequence,
                                'scriptSig': {
                                    'hex': bitcoin.core.b2x(bytes(input.scriptSig))
                                }
                            }
                            for input in transaction.vin],
                        'vout': [{
                            'value': output.nValue,
                            'n': index,
                            'scriptPubKey': {
                                'hex': bitcoin.core.b2x(bytes(output.scriptPubKey))
                            }
                        }
                        for index, output in enumerate(transaction.vout)]
                    }
                else:
                    return transaction
        else:
            def get_transaction_json(transaction):
                if isinstance(transaction, bitcoin.core.CTransaction):
                    return bitcoin.core.b2x(transaction.serialize())
                else:
                    return transaction

        return get_transaction_json

    def _run_rpc_server(self):
        """
        Starts the JSON/RPC server.
        """
        if not self.configuration.rpc_enabled:
            self.output.write("Error: RPC must be enabled in the configuration.\n")
            return

        def create_server():
            return RpcServer(
                self.controller, self.configuration, self.event_loop, self.cache_factory,
                keep_alive=60, debug=True, allowed_methods=('POST',))

        aiohttp.HttpMessage.SERVER_SOFTWARE = 'Colorcore/{version}'.format(version=colorcore.__version__)
        root_future = self.event_loop.create_server(create_server, '', self.configuration.rpc_port)
        self.event_loop.run_until_complete(root_future)
        self.output.write("Starting RPC server on port {port}...\n".format(port=self.configuration.rpc_port))
        self.event_loop.run_forever()

    def parse(self, args):
        """
        Parses the arguments and executes the corresponding operation.

        :param list[str] args: The arguments to parse.
        """
        args = vars(self._parser.parse_args(args))
        func = args.pop('_func', self._parser.print_usage)
        func(**args)


class ControllerError(Exception):
    """A known error occurred while executing the operation."""
    pass