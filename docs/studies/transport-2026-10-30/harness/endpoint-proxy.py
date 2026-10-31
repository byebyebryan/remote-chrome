import asyncio,sys,pathlib
async def connection(reader,writer):
 other=None
 try:
  rr,other=await asyncio.open_unix_connection(sys.argv[1],limit=65536)
  async def pump(r,w):
   while data:=await r.read(65536):w.write(data);await w.drain()
   w.close()
  await asyncio.gather(pump(reader,other),pump(rr,writer))
 except (OSError,asyncio.CancelledError):pass
 finally:
  writer.close()
  if other:other.close()
async def main():
 server=await asyncio.start_server(connection,'198.18.210.5',22345,limit=65536)
 pathlib.Path(sys.argv[2]).write_text('ready')
 async with server:await server.serve_forever()
asyncio.run(main())
