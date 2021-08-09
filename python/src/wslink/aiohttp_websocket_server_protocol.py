import asyncio
import inspect
import json
import logging
import re
import sys
import traceback
import uuid

import aiohttp
import aiohttp.web as aiohttp_web

from . import schedule_coroutine
from wslink import publish as pub


async def _on_startup(app):
    # Emit an expected log message so launcher.py knows we've started up.
    logging.critical("wslink: Starting factory")
    # We've seen some issues with stdout buffering - be conservative.
    sys.stdout.flush()

    # Also schedule server shutdown in case no clients connect within timeout
    _schedule_shutdown(app)


def _schedule_shutdown(app):
    timeout = app["state"]["server_config"]["timeout"]
    app["state"]["shutdown_task"] = schedule_coroutine(timeout, _stop_server, app)


async def _stop_server(app):
    # Disconnecting any connected clients of handler(s)
    for route, handler in app["state"]["server_config"]["ws"].items():
        await handler.disconnectClients()

    # Neither site.stop() nor runner.cleanup() actually stop the server
    # as documented, but at least runner.cleanup() results in the
    # "on_shutdown" signal getting sent.
    logging.info("Performing runner.cleanup()")
    await app["state"]["runner"].cleanup()

    # So to actually stop the server, the workaround is just to resolve
    # the future we awaited in the start method.
    logging.info("Stopping server")
    app["state"]["running"].set_result(True)


def _fix_path(path):
    if not path.startswith("/"):
        return "/{0}".format(path)
    return path


class AiohttpWslinkServer(object):
    def __init__(self):
        self.app = None
        self.config = None

    def get_app(self):
        return self.app

    def set_app(self, app):
        self.app = app

    def get_config(self):
        return self.config

    def set_config(self, config):
        self.config = config

    async def start(self):
        app = self.app
        server_config = self.config
        host = self.config["host"]
        port = int(self.config["port"])
        timeout = int(self.config["timeout"])
        handle_signals = self.config["handle_signals"]

        runner = aiohttp_web.AppRunner(app, handle_signals=handle_signals)
        loop = asyncio.get_running_loop()
        running = loop.create_future()

        app["state"]["running"] = running
        app["state"]["runner"] = runner

        logging.info("awaiting runner setup")
        await runner.setup()

        my_site = aiohttp_web.TCPSite(runner, host, port)

        logging.info("awaiting site startup")
        await my_site.start()

        logging.info("awaiting running future")
        await running

    async def stop(self):
        await _stop_server(self.app)


def create_wslink_server(server_config):
    web_app = aiohttp_web.Application()

    if "ws" in server_config:
        ws_routes = server_config["ws"]
        routes = []

        for route, server_protocol in ws_routes.items():
            protocol_handler = WslinkHandler(server_protocol)
            ws_routes[route] = protocol_handler
            routes.append(
                aiohttp_web.get(_fix_path(route), protocol_handler.handleWsRequest)
            )

        web_app.add_routes(routes)

    if "static" in server_config:
        static_routes = server_config["static"]
        routes = []

        for route, server_path in static_routes.items():
            routes.append(aiohttp_web.static(_fix_path(route), server_path))

        web_app.add_routes(routes)

    if "logging_level" in server_config and server_config["logging_level"]:
        logging.basicConfig(level=server_config["logging_level"])

    web_app.on_startup.append(_on_startup)

    web_app["state"] = {}
    web_app["state"]["server_config"] = server_config

    server = AiohttpWslinkServer()
    server.set_app(web_app)
    server.set_config(server_config)

    return server


# -----------------------------------------------------------------------------
# WS protocol definition
# -----------------------------------------------------------------------------


class WslinkHandler(object):
    def __init__(self, protocol=None):
        self.serverProtocol = protocol
        self.functionMap = {}
        self.attachmentsReceived = {}
        self.attachmentsRecvQueue = []
        self.connections = {}

    def setServerProtocol(self, protocol):
        self.serverProtocol = protocol

    def getServerProtocol(self):
        return self.serverProtocol

    async def disconnectClients(self):
        logging.info("Closing client connections:")
        keys = list(self.connections.keys())
        for client_id in keys:
            logging.info("  {0}".format(client_id))
            ws = self.connections[client_id]
            await ws.close(
                code=aiohttp.WSCloseCode.GOING_AWAY, message="Server shutdown"
            )

    async def handleWsRequest(self, request):
        aiohttp_app = request.app

        client_id = str(uuid.uuid4()).replace("-", "")
        current_ws = aiohttp_web.WebSocketResponse()
        self.connections[client_id] = current_ws

        logging.info("client {0} connected".format(client_id))

        if aiohttp_app["state"]["shutdown_task"]:
            logging.info("Canceling shutdown task")
            aiohttp_app["state"]["shutdown_task"].cancel()
            aiohttp_app["state"]["shutdown_task"] = None

        await current_ws.prepare(request)

        await self.onConnect(request)

        async for msg in current_ws:
            await self.onMessage(msg, client_id)

        await self.onClose()

        del self.connections[client_id]

        logging.info("client {0} disconnected".format(client_id))

        if not self.connections:
            logging.info("No more connections, scheduling shutdown")
            _schedule_shutdown(aiohttp_app)

        return current_ws

    async def onConnect(self, request):
        aiohttp_app = request.app

        # Build the rpc method dictionary. self.serverProtocol isn't set until connected.
        if not self.getServerProtocol():
            return
        protocolList = self.getServerProtocol().getLinkProtocols()
        protocolList.append(self.getServerProtocol())
        for protocolObject in protocolList:
            protocolObject.init(
                self.publish,
                self.addAttachment,
                lambda: schedule_coroutine(0, _stop_server, aiohttp_app),
            )
            test = lambda x: inspect.ismethod(x) or inspect.isfunction(x)
            for k in inspect.getmembers(protocolObject.__class__, test):
                proc = k[1]
                if "_wslinkuris" in proc.__dict__:
                    uri_info = proc.__dict__["_wslinkuris"][0]
                    if "uri" in uri_info:
                        uri = uri_info["uri"]
                        self.functionMap[uri] = (protocolObject, proc)
        pub.publishManager.registerProtocol(self)

    async def onClose(self):
        pub.publishManager.unregisterProtocol(self)

    async def handleSystemMessage(self, rpcid, methodName, args, client_id):
        rpcList = rpcid.split(":")
        if rpcList[0] == "system":
            if methodName == "wslink.hello":
                if (
                    args
                    and args[0]
                    and (type(args[0]) is dict)
                    and ("secret" in args[0])
                    and (args[0]["secret"] == self.getServerProtocol().secret)
                ):
                    await self.sendWrappedMessage(
                        rpcid,
                        {"clientID": "c{0}".format(client_id)},
                        client_id=client_id,
                    )
                else:
                    await self.sendWrappedError(
                        rpcid,
                        pub.AUTHENTICATION_ERROR,
                        "Authentication failed",
                        client_id=client_id,
                    )
            else:
                await self.sendWrappedError(
                    rpcid,
                    pub.METHOD_NOT_FOUND,
                    "Unknown system method called",
                    client_id=client_id,
                )
            return True
        return False

    async def onMessage(self, msg, client_id):
        isBinary = msg.type == aiohttp.WSMsgType.BINARY
        payload = msg.data

        if isBinary:
            # assume all binary messages are attachments
            try:
                key = self.attachmentsRecvQueue.pop(0)
                self.attachmentsReceived[key] = payload
            except:
                pass
            return

        # handles issue https://bugs.python.org/issue10976
        # `payload` is type bytes in Python 3. Unfortunately, json.loads
        # doesn't support taking bytes until Python 3.6.
        if type(payload) is bytes:
            payload = payload.decode("utf-8")

        rpc = json.loads(payload)
        logging.debug("wslink incoming msg %s" % payload)
        if "id" not in rpc:
            # should be a binary attachment header
            if rpc.get("method") == "wslink.binary.attachment":
                keys = rpc.get("args", [])
                if isinstance(keys, list):
                    for k in keys:
                        # wait for an attachment by it's order
                        self.attachmentsRecvQueue.append(k)
            return

        # TODO validate
        version = rpc["wslink"]
        rpcid = rpc["id"]
        methodName = rpc["method"]

        args = []
        kwargs = {}
        if ("args" in rpc) and isinstance(rpc["args"], list):
            args = rpc["args"]
        if ("kwargs" in rpc) and isinstance(rpc["kwargs"], dict):
            kwargs = rpc["kwargs"]

        # Check for system messages, like hello
        if await self.handleSystemMessage(rpcid, methodName, args, client_id):
            return

        if not methodName in self.functionMap:
            await self.sendWrappedError(
                rpcid,
                pub.METHOD_NOT_FOUND,
                "Unregistered method called",
                methodName,
                client_id=client_id,
            )
            return

        obj, func = self.functionMap[methodName]
        try:
            # get any attachments
            def findAttachments(o):
                if (
                    isinstance(o, str)
                    and re.match(r"^wslink_bin\d+$", o)
                    and o in self.attachmentsReceived
                ):
                    attachment = self.attachmentsReceived[o]
                    del self.attachmentsReceived[o]
                    return attachment
                elif isinstance(o, list):
                    for i, v in enumerate(o):
                        o[i] = findAttachments(v)
                elif isinstance(o, dict):
                    for k in o:
                        o[k] = findAttachments(o[k])
                return o

            args = findAttachments(args)
            kwargs = findAttachments(kwargs)

            args.insert(0, obj)

            try:
                results = await asyncio.coroutine(func)(*args, **kwargs)
                await self.sendWrappedMessage(
                    rpcid, results, method=methodName, client_id=client_id
                )
            except Exception as e_inst:
                captured_trace = traceback.format_exc()
                logging.error("Exception raised")
                logging.error(repr(e_inst))
                logging.error(captured_trace)
                await self.sendWrappedError(
                    rpcid,
                    pub.EXCEPTION_ERROR,
                    "Exception raised",
                    {
                        "method": methodName,
                        "exception": repr(e_inst),
                        "trace": captured_trace,
                    },
                    client_id=client_id,
                )

        except Exception as e:
            await self.sendWrappedError(
                rpcid,
                pub.EXCEPTION_ERROR,
                "Exception raised",
                {
                    "method": methodName,
                    "exception": repr(e),
                    "trace": traceback.format_exc(),
                },
                client_id=client_id,
            )
            return

    async def sendWrappedMessage(self, rpcid, content, method="", client_id=None):
        wrapper = {
            "wslink": "1.0",
            "id": rpcid,
            "result": content,
        }
        try:
            encMsg = json.dumps(wrapper, ensure_ascii=False)
        except TypeError as e:
            # the content which is not serializable might be arbitrarily large, don't include.
            # repr(content) would do that...
            await self.sendWrappedError(
                rpcid,
                pub.RESULT_SERIALIZE_ERROR,
                "Method result cannot be serialized",
                method,
                client_id=client_id,
            )
            return

        websockets = (
            [self.connections[client_id]]
            if client_id
            else [self.connections[c] for c in self.connections]
        )

        # Check if any attachments in the map go with this message
        attachments = pub.publishManager.getAttachmentMap()
        found_keys = []
        if attachments:
            for key in attachments:
                # string match the encoded attachment key
                if key in encMsg:
                    if key not in found_keys:
                        found_keys.append(key)
                    # increment  for key
                    pub.publishManager.registerAttachment(key)
                    # send header
                    header = {
                        "wslink": "1.0",
                        "method": "wslink.binary.attachment",
                        "args": [key],
                    }
                    json_header = json.dumps(header, ensure_ascii=False)
                    for ws in websockets:
                        await ws.send_str(json_header)
                        # Send binary message
                        await ws.send_bytes(attachments[key])

                    # decrement for key
                    pub.publishManager.unregisterAttachment(key)

            pub.publishManager.freeAttachments(keys=found_keys)

        for ws in websockets:
            await ws.send_str(encMsg)

    async def sendWrappedError(self, rpcid, code, message, data=None, client_id=None):
        wrapper = {
            "wslink": "1.0",
            "id": rpcid,
            "error": {
                "code": code,
                "message": message,
            },
        }
        if data:
            wrapper["error"]["data"] = data
        encMsg = json.dumps(wrapper, ensure_ascii=False)
        websockets = (
            [self.connections[client_id]]
            if client_id
            else [self.connections[c] for c in self.connections]
        )
        for ws in websockets:
            await ws.send_str(encMsg)

    def publish(self, topic, data, client_id=None):
        client_list = [client_id] if client_id else [c_id for c_id in self.connections]
        for client in client_list:
            pub.publishManager.publish(topic, data, client_id=client)

    def addAttachment(self, payload):
        return pub.publishManager.addAttachment(payload)

    def setSecret(self, newSecret):
        self.secret = newSecret
