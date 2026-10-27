import asyncio,json,os,secrets,time,random
from aiohttp import web,WSMsgType

app=web.Application(client_max_size=8192)
ROOMS={}
TYPES=["时间","地点","歌女","凶器","动机"]
NAMES=["午夜钟声","二楼包厢","红玫瑰","银色手枪","失踪账本"]
MAX=8
def code():return "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789",k=6))
def fail(message,status=400):raise web.HTTPBadRequest(text=json.dumps({"error":message},ensure_ascii=False),content_type="application/json")
def view(room,p):
 return {"code":room["code"],"phase":room["phase"],"round":room["round"],"target":room.get("target"),"host":room["host"],"you":p["id"],"role":p.get("role") if room["phase"]!="lobby" else None,"hand":p.get("hand",[]) if room["phase"] not in ("lobby","end") else [],"peek":p.get("peek"),"picked":p.get("picked"),"vote":p.get("vote"),"players":[{"id":x["id"],"name":x["name"],"bot":x["bot"],"online":x["online"],"ready":x["ready"],"done":x.get("done",False),"handCount":len(x.get("hand",[])),"role":x.get("role") if room["phase"]=="end" else None,"vote":x.get("vote") if room["phase"]=="end" else None} for x in room["players"]],"reveal":room.get("reveal",[]),"history":room["history"],"log":room["log"][-60:],"winner":room.get("winner")}
def player(room,token):
 return next((p for p in room["players"] if p["token"]==token),None)
async def broadcast(room):
 for p in room["players"]:
  ws=p.get("ws")
  if ws and not ws.closed:
   try:await ws.send_json({"type":"state","room":view(room,p)})
   except Exception:pass
def human(room):return [p for p in room["players"] if not p["bot"]]
def bots_act(room):
 if room["phase"]=="investigate":
  for p in room["players"]:
   if p["bot"]:
    others=[x for x in room["players"] if x!=p and x["hand"]]
    x=random.choice(others);c=random.choice(x["hand"])
    p["peek"]={"name":x["name"],"type":c["type"],"label":c["label"]};p["done"]=True
 if room["phase"]=="submit":
  for p in room["players"]:
   if p["bot"]:
    valid=[c for c in p["hand"] if (c["type"]!=room["target"] if p["role"]=="真凶" else c["type"]==room["target"])]
    if not valid:valid=p["hand"]
    if valid:
     c=random.choice(valid);p["hand"].remove(c);room["pending"].append({"type":c["type"],"label":c["label"]});p["done"]=True
 if room["phase"]=="vote":
  for p in room["players"]:
   if p["bot"]:
    others=[x for x in room["players"] if x!=p]
    p["vote"]=random.choice(others)["id"];p["done"]=True
def advance(room):
 for _ in range(10):
  bots_act(room)
  if not all(p.get("done") for p in room["players"]):break
  phase=room["phase"]
  if phase=="investigate":room["phase"]="submit"
  elif phase=="submit":
   cards=room["pending"];random.shuffle(cards);room["reveal"]=cards[:];room["history"].append({"round":room["round"],"target":room["target"],"cards":cards[:]});room["phase"]="reveal"
   break
  elif phase=="vote":
   killer=next(p for p in room["players"] if p["role"]=="真凶")
   room["winner"]="良人" if all(p["vote"]==killer["id"] for p in room["players"] if p["role"]=="良人") else "真凶"
   room["phase"]="end";break
  else:break
  for p in room["players"]:p["done"]=False
def start(room):
 ps=room["players"];roles=["真凶","帮凶"]+["良人"]*(len(ps)-2);random.shuffle(roles)
 for p,r in zip(ps,roles):
  p.update(role=r,hand=[{"id":secrets.token_hex(4),"type":t,"label":NAMES[i]} for i,t in enumerate(TYPES)],done=False,peek=None,picked=None,vote=None)
 room.update(phase="investigate",round=1,target=random.choice(TYPES),pending=[],reveal=[],history=[],winner=None)
 advance(room)
def validate(room,p,act):
 phase=room["phase"];who=act.get("action")
 if who=="ready" and phase=="lobby":p["ready"]=not p["ready"]
 elif who=="bot" and phase=="lobby" and p["id"]==room["host"]:
  if len(room["players"])>=MAX:fail("房间最多8人")
  room["players"].append({"id":secrets.token_hex(6),"token":secrets.token_urlsafe(24),"name":"模拟宾客 "+str(len(room["players"])),"bot":True,"online":True,"ready":True})
 elif who=="removeBot" and phase=="lobby" and p["id"]==room["host"]:
  room["players"]=[x for x in room["players"] if x["id"]!=act.get("playerId") or not x["bot"]]
 elif who=="start" and phase=="lobby" and p["id"]==room["host"]:
  if len(room["players"])<4:fail("至少需要4位玩家（可加入AI）")
  if not all(x["ready"] for x in human(room)):fail("请等待真人玩家准备")
  start(room)
 elif who=="inspect" and phase=="investigate" and not p.get("done"):
  other=next((x for x in room["players"] if x["id"]==act.get("playerId") and x!=p),None)
  if not other:fail("请选择其他宾客")
  card=next((c for c in other["hand"] if c["id"]==act.get("cardId")),None)
  if not card:fail("这张牌已不存在")
  p["peek"]={"name":other["name"],"type":card["type"],"label":card["label"]};p["done"]=True
 elif who=="submit" and phase=="submit" and not p.get("done"):
  card=next((c for c in p["hand"] if c["id"]==act.get("cardId")),None)
  if not card:fail("手牌无效")
  if (card["type"]==room["target"])!=(p["role"]!="真凶"):fail("身份出牌规则不允许这张牌")
  p["hand"].remove(card);room["pending"].append({"type":card["type"],"label":card["label"]});p["done"]=True
 elif who=="next" and phase=="reveal":
  if p["id"]!=room["host"]:fail("由房主开启下一轮")
  if room["round"]>=3:room["phase"]="vote"
  else:room["round"]+=1;room["phase"]="investigate";room["target"]=random.choice(TYPES)
  for x in room["players"]:x["done"]=False
 elif who=="vote" and phase=="vote" and not p.get("done"):
  target=next((x for x in room["players"] if x["id"]==act.get("playerId") and x!=p),None)
  if not target:fail("请选择其他宾客")
  p["vote"]=target["id"];p["done"]=True
 elif who=="say":
  txt=str(act.get("text","")).strip()[:240]
  if not txt:fail("消息不能为空")
  room["log"].append({"name":p["name"],"text":txt})
 elif who=="reset" and phase=="end" and p["id"]==room["host"]:
  room.update(phase="lobby",round=0,target=None,reveal=[],history=[],winner=None)
  for x in room["players"]:x.update(ready=x["bot"],done=False,peek=None,hand=[],role=None)
 else:fail("当前阶段不允许这个操作")
 advance(room)
async def index(req):return web.FileResponse(os.path.join(os.path.dirname(__file__),"multiplayer.html"))
async def health(req):return web.json_response({"status":"ok","rooms":len(ROOMS)})
async def create(req):
 data=await req.json();name=str(data.get("name","玩家")).strip()[:20] or "玩家"
 c=code()
 while c in ROOMS:c=code()
 p={"id":secrets.token_hex(6),"token":secrets.token_urlsafe(32),"name":name,"bot":False,"online":False,"ready":False}
 room={"code":c,"host":p["id"],"players":[p],"phase":"lobby","round":0,"target":None,"history":[],"log":[],"reveal":[],"created":time.time()}
 ROOMS[c]=room
 return web.json_response({"code":c,"token":p["token"],"playerId":p["id"]})
async def join(req):
 data=await req.json();c=str(data.get("code","")).upper().strip();room=ROOMS.get(c)
 if not room:fail("房间不存在")
 if room["phase"]!="lobby":fail("游戏已经开始")
 if len(room["players"])>=MAX:fail("房间已满")
 name=str(data.get("name","玩家")).strip()[:20] or "玩家"
 p={"id":secrets.token_hex(6),"token":secrets.token_urlsafe(32),"name":name,"bot":False,"online":False,"ready":False}
 room["players"].append(p);await broadcast(room)
 return web.json_response({"code":c,"token":p["token"],"playerId":p["id"]})
async def websocket(req):
 ws=web.WebSocketResponse(heartbeat=20,max_msg_size=4096);await ws.prepare(req)
 room=None;p=None
 try:
  async for m in ws:
   if m.type!=WSMsgType.TEXT:continue
   try:
    data=json.loads(m.data)
    if p is None:
     if data.get("type")!="hello":fail("请先验证身份")
     room=ROOMS.get(str(data.get("code","")).upper())
     if not room:fail("房间已失效")
     p=player(room,data.get("token"))
     if not p:fail("会话凭证无效")
     old=p.get("ws");p["ws"]=ws;p["online"]=True
     if old and old!=ws and not old.closed:await old.close()
    else:
     if data.get("type")!="action":fail("未知协议")
     validate(room,p,data.get("payload") or {})
    await broadcast(room)
   except web.HTTPException as e:
    await ws.send_json({"type":"error","message":json.loads(e.text)["error"]})
   except Exception:
    await ws.send_json({"type":"error","message":"请求处理失败"})
 finally:
  if p and p.get("ws")==ws:
   p["online"]=False;p["ws"]=None
   remaining=[x for x in human(room) if x["online"]]
   if p["id"]==room["host"] and remaining:room["host"]=remaining[0]["id"]
   if room["phase"] in ("investigate","submit","vote") and not p.get("done"):
    if room["phase"]=="investigate":p["done"]=True
    elif room["phase"]=="submit":
     valid=[c for c in p["hand"] if (c["type"]!=room["target"] if p["role"]=="真凶" else c["type"]==room["target"])]
     if valid:
      card=random.choice(valid);p["hand"].remove(card);room["pending"].append({"type":card["type"],"label":card["label"]});p["done"]=True
    elif room["phase"]=="vote":
     others=[x for x in room["players"] if x!=p]
     p["vote"]=random.choice(others)["id"];p["done"]=True
    advance(room)
   await broadcast(room)
 return ws
app.router.add_get("/",index);app.router.add_get("/health",health);app.router.add_post("/api/rooms",create);app.router.add_post("/api/join",join);app.router.add_get("/ws",websocket)
if __name__=="__main__":web.run_app(app,port=int(os.getenv("PORT",8765)),host="0.0.0.0")
