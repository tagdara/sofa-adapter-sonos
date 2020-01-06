
from aiohttp import web

async def handle(request):
    print(request)
    text = "OK"
    return web.Response(text=text)

app = web.Application()
app.router.add_get('/', handle)
app.router.add_get('/{name}', handle)

web.run_app(app, port=9999)