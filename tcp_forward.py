import asyncio

REMOTE_HOST = "api.jumengai.net"
REMOTE_PORT = 443
LOCAL_PORT = 19443

async def forward(reader, writer):
    try:
        while True:
            data = await reader.read(8192)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except:
        pass
    finally:
        writer.close()

async def handle(local_reader, local_writer):
    try:
        remote_reader, remote_writer = await asyncio.open_connection(REMOTE_HOST, REMOTE_PORT)
    except Exception as e:
        local_writer.close()
        return
    await asyncio.gather(
        forward(local_reader, remote_writer),
        forward(remote_reader, local_writer),
    )

async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", LOCAL_PORT)
    print(f"Forwarding 0.0.0.0:{LOCAL_PORT} -> {REMOTE_HOST}:{REMOTE_PORT}")
    async with server:
        await server.serve_forever()

asyncio.run(main())
