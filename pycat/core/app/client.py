"""In-process client port. Views consume projections and explicit application actions."""
import uuid


class LocalClient:
    def __init__(self, services, *, source='tui'):
        self.services, self.source = services, source

    async def bootstrap(self):
        return {'operations': self.services.workbench.catalog(), 'runs': self.services.interactive.list()}

    async def operation(self, name, arguments):
        return await self.services.interactive.operation(name, arguments, request_id=uuid.uuid4().hex, source=self.source)

    async def submit(self, request, *, request_id):
        return await self.services.interactive.submit(request, request_id=request_id, source=self.source)

    async def snapshot(self, run_id):
        return self.services.interactive.snapshot(run_id)

    def events(self, run_id, *, after=0):
        return self.services.interactive.events(run_id, after=after)

    async def cancel(self, run_id):
        return self.services.interactive.cancel(run_id)

    async def guidance(self, run_id, text):
        return self.services.interactive.guidance(run_id, text)

    async def respond(self, run_id, interaction_id, decision):
        return self.services.interactive.respond(run_id, interaction_id, decision)
