import aiohttp
from typing import *
import asyncio
from . import logger
from ujson import dumps, loads
from sanic.request import Request


class ServerOffline(Exception):
    pass

class NodeInstance:
    def __init__(self, url: str):
        self.url: str = url
        self.session = aiohttp.ClientSession(headers={'Content-type': 'application/json'}, json_serialize=dumps)
        self.status: bool = False
        self.dispatch = logger.dispatch
    
    async def set_online(self):
        if self.status:
            return
        self.status = True
        await self.dispatch('node_online', self.url)
    
    async def set_offline(self):
        if not self.status:
            return
        self.status = False
        await self.dispatch('node_offline', self.url)

    async def check_alive(self) -> bool:
        try:
            async with self.session.post(self.url, json={'jsonrpc': '2.0', 'method': 'eth_syncing', 'params': [], 'id': 1}, timeout=10) as resp:
                if (await resp.json())['result']:
                    await self.set_offline()
                    return False
                else:
                    await self.set_online()
                    return True

        except:
            await self.set_offline()
            return False
    
    async def do_request(self, data: Dict[str, Any]=None) -> Union[Tuple[str, int, str], ServerOffline]:
        try:
            async with self.session.post(self.url, data=data, timeout=3) as resp:   # if we go higher than 3, we may be more counter-productive waiting for all the nodes to respond 
                return (await resp.text(), resp.status, dumps(dict(resp.headers)))
        except (aiohttp.ServerTimeoutError, aiohttp.ServerConnectionError, aiohttp.ClientConnectionError, aiohttp.ClientOSError, aiohttp.ClientResponseError):
            await self.set_offline()
            return ServerOffline()
    
    async def stop(self):
        await self.session.close()

class OutOfAliveNodes:
    pass


class NodeRouter:
    def __init__(self, urls: List[str]):
        if not urls:
            raise ValueError('No nodes provided')
        self.urls = urls
        self.dispatch = logger.dispatch
        self.listener = logger.listener
    
    async def recheck(self) -> List[NodeInstance]: # returns a list of alive nodes
        tasks = [node.check_alive() for node in self.nodes]
        results = await asyncio.gather(*tasks)
        self.alive_count = results.count(True)  
        self.dead_count = len(self.nodes) - self.alive_count
        self.index = 0
        return [node for node in self.nodes if node.status]
    
    async def repeat_check(self) -> None:
        while True:
            await self.recheck()
            await asyncio.sleep(60)

    async def setup(self) -> None:
        self.nodes: List[NodeInstance] = [NodeInstance(url) for url in self.urls]
        await self.recheck()
        await self.dispatch('node_router_online')
    
    async def get_execution_node(self) -> NodeInstance:
        # get the same node, if offline, add 1 to the index and try again
        if self.alive_count == 0:
            raise OutOfAliveNodes()
        if self.index >= len(self.nodes):
            self.index = 0
        node = self.nodes[self.index]
        if not node.status:
            self.index += 1
            return await self.get_execution_node()
        return node

    # https://github.com/ethereum/execution-apis/blob/main/src/engine/specification.md#load-balancing-and-advanced-configurations=

    # CL will be the one contacting us, and we route it to the node
    # - Choosing a payload from the getPayload responses (just picking the first is the easiest solution, choosing the most profitable is ideal but much harder).
    # - Selecting a response from newPayload and forkchoiceUpdated and ensuring they don't conflict.
    # - Detecting poor service from the nodes and switching between them.

    # debated: Regaring picking responses for newPayload and forkchoiceUpdated, the CL probably wants to try and stick with the same one, for consistency. Then switch over when the primary one is determined to have poor quality of service.
    async def do_engine_route(self, req: Request) -> None:
        if req.json['method'] == 'engine_getPayloadV1':    # right now we just get one payload but later we will pick the most profitable one
            n = await self.get_alive_node()     # old code
            r = await n.do_request(req.body)
            resp = await req.respond(status=r[1])
            await resp.send(r[0], end_stream=True)
        else:
            await self.route(req)
    
    async def route(self, req: Request) -> None:
        if req.json['method'] == 'engine_forkchoiceUpdatedV1':
            # wait for just one node to respond but send it to all
            n = await self.get_execution_node()
            r = await n.do_request(req.body)
            [asyncio.create_task(node.do_request(req.body)) for node in self.nodes if node.status and node != n]
            resp = await req.respond(status=r[1], headers=loads(r[2]))
            await resp.send(r[0], end_stream=True)
            return
            

        # send the request to all nodes
        n = await self.get_execution_node()
        r = await n.do_request(req.body)
        if isinstance(r, ServerOffline):
            resp = await req.respond(status=r[1], headers=loads(r[2]))
            await resp.send(dumps({'error': 'no upstream nodes'}), end_stream=True)
            return

        # send the response
        resp = await req.respond(status=r[1], headers=loads(r[2]))
        await resp.send(r[0], end_stream=True)
    
    async def stop(self) -> None:
        tasks = [node.stop() for node in self.nodes]
        await asyncio.gather(*tasks)
