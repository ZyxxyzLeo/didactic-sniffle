"""Night Banquet: server-authoritative, unofficial 4-8 player deduction game."""
import asyncio, json, math, os, random, re, secrets, tempfile, time
from pathlib import Path
from aiohttp import web, WSMsgType

TYPES = ["时间", "地点", "人物", "凶器", "动机"]
LABELS = {
    "时间": ["午夜钟响","开场铃响","雨停时分","散场之前","凌晨一点","掌灯时刻","第二幕终","晨雾初起"],
    "地点": ["二楼包厢","后台走廊","暗红舞厅","道具库房","花园露台","西侧楼梯","旧琴房","后巷车库"],
    "人物": ["红玫瑰","沈大班","林医生","月笙","顾记者","魏管家","苏舞姬","夏琴师"],
    "凶器": ["折断发簪","银质手枪","掺药酒杯","钢琴弦线","舞台短刃","猩红丝巾","裂纹烟斗","古董袖扣"],
    "动机": ["失踪账本","旧日背叛","巨额保单","失窃唱片","未寄情书","过期债券","秘密录音","家族遗嘱"]
}
BOT_NAMES = ["陆警探","沈老板","顾记者","苏舞姬","魏管家","林医生","夏琴师"]
ROOMS = {}
SOCKETS = {}
STORE = Path(os.environ.get("SGM_ROOM_STORE", "/tmp/shenggeman-rooms-v3.json"))
MAX_ROOMS, MAX_PLAYERS, GRACE = 100, 8, 28
app = web.Application(client_max_size=8192)

def now(): return time.time()
def reject(message, status=400):
    raise web.HTTPBadRequest(text=json.dumps({"error":message},ensure_ascii=False), content_type="application/json")
def safe_name(x):
    if not isinstance(x,str):reject("请填写昵称")
    x=re.sub(r"[\x00-\x1f\x7f<>]","",x).strip()
    if not 1 <= len(x) <= 14:reject("昵称为 1–14 个字符")
    return x
def new_code():
    return "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
def person(name,bot=False):
    return {"id":secrets.token_hex(6),"token":secrets.token_urlsafe(26) if not bot else "",
            "name":name,"bot":bot,"ready":bot,"role":None,"hand":[],
            "done":False,"peek":None,"journal":[],"vote":None,"online_since":None,"offline_since":None}
def clean_room(code):
    if not isinstance(code,str):reject("房间码无效")
    c=code.strip().upper()
    if not re.fullmatch(r"[A-HJ-NP-Z2-9]{6}",c):reject("请输入 6 位房间码")
    room=ROOMS.get(c)
    if not room:reject("房间不存在或已过期，请重新创建")
    return room
def connected(p):
    return (p["id"] in SOCKETS) or p["bot"]
def save():
    try:
        STORE.parent.mkdir(parents=True,exist_ok=True)
        data={"version":3,"rooms":list(ROOMS.values())}
        fd,path=tempfile.mkstemp(dir=STORE.parent,prefix=".sgm-",suffix=".json")
        try:
            with os.fdopen(fd,"w",encoding="utf8") as f:
                json.dump(data,f,ensure_ascii=False,separators=(",",":"))
                f.flush()
            os.replace(path,STORE)
        finally:
            if os.path.exists(path):os.unlink(path)
    except Exception as exc:print("save failed:",type(exc).__name__,flush=True)
def load():
    if not STORE.exists():return
    try:
        data=json.loads(STORE.read_text(encoding="utf8"))
        if data.get("version")!=3:return
        for r in data.get("rooms",[]):
            if r.get("phase") in ("lobby","investigate","submit","reveal","vote","end"):
                for p in r["players"]:
                    if not p["bot"]:p["offline_since"]=now()
                ROOMS[r["code"]]=r
    except Exception as exc:print("load failed:",type(exc).__name__,flush=True)
load()

def view(room,p):
    end=room["phase"]=="end"
    return {"code":room["code"],"phase":room["phase"],"round":room["round"],"target":room.get("target"),
        "host":room["host"],"you":p["id"],"role":p["role"] if room["phase"]!="lobby" else None,
        "hand":p["hand"] if room["phase"] not in ("lobby","end") else [],
        "peek":p["peek"],"journal":p["journal"],
        "players":[{"id":x["id"],"name":x["name"],"bot":x["bot"],"online":connected(x),
            "ready":x["ready"],"done":x["done"],"handCount":len(x["hand"]),
            "slots":sorted(c["slot"] for c in x["hand"]) if room["phase"]=="investigate" else [],
            "role":x["role"] if end else None,"vote":x["vote"] if end else None} for x in room["players"]],
        "reveal":room["reveal"],"history":room["history"],"chat":room["chat"][-50:],
        "winner":room["winner"],"threshold":room.get("threshold",0),
        "version":3}
def get_user(room,token):
    if not isinstance(token,str):return None
    return next((p for p in room["players"] if not p["bot"] and secrets.compare_digest(p["token"],token)),None)
async def broadcast(room):
    for p in room["players"]:
        ws=SOCKETS.get(p["id"])
        if ws and not ws.closed:
            try:await ws.send_json({"type":"state","room":view(room,p)})
            except (ConnectionError,RuntimeError):pass
def log(room,name,text):
    room["chat"].append({"name":name,"text":text,"ts":int(now())})
    room["chat"]=room["chat"][-70:]
def bot_act(room):
    phase=room["phase"]
    for p in room["players"]:
        if not p["bot"] and (p["offline_since"] is None or now()-p["offline_since"]<GRACE):continue
        if p["done"]:continue
        if phase=="investigate":
            others=[x for x in room["players"] if x!=p and x["hand"]]
            if others:
                other=random.choice(others);card=random.choice(other["hand"])
                glimpse={"round":room["round"],"name":other["name"],"slot":card["slot"],"type":card["type"],"label":card["label"]}
                p["peek"]=glimpse;p["journal"].append(glimpse)
            p["done"]=True
        elif phase=="submit":
            legal=[c for c in p["hand"] if (c["type"]!=room["target"] if p["role"]=="真凶" else c["type"]==room["target"])]
            if legal:
                c=random.choice(legal);p["hand"].remove(c);room["pending"].append({"type":c["type"],"label":c["label"]});p["done"]=True
        elif phase=="reveal":p["done"]=True
        elif phase=="vote":
            others=[x for x in room["players"] if x!=p]
            p["vote"]=random.choice(others)["id"];p["done"]=True
def advance(room):
    for _ in range(10):
        if room["phase"] in ("lobby","end"):break
        bot_act(room)
        if not all(p["done"] for p in room["players"]):break
        phase=room["phase"]
        if phase=="investigate":
            room["phase"]="submit"
        elif phase=="submit":
            cards=room["pending"][:];random.shuffle(cards)
            room["reveal"]=cards
            room["history"].append({"round":room["round"],"target":room["target"],"cards":cards})
            room["phase"]="reveal"
        elif phase=="reveal":
            if room["round"]>=3:room["phase"]="vote"
            else:
                room["round"]+=1;room["phase"]="investigate"
                room["target"]=room["targets"][room["round"]-1]
                room["pending"]=[];room["reveal"]=[]
        elif phase=="vote":
            killer=next(x for x in room["players"] if x["role"]=="真凶")
            correct=sum(p["vote"]==killer["id"] for p in room["players"] if p["role"]=="良人")
            room["winner"]="良人" if correct>=room["threshold"] else "真凶"
            room["phase"]="end"
        for p in room["players"]:p["done"]=False
def start_game(room):
    n=len(room["players"])
    if n<4:reject("至少需要 4 人；可添加 AI 补位")
    if any(not connected(p) or not p["ready"] for p in room["players"] if not p["bot"]):reject("等待所有真人在线并准备")
    roles=["真凶"]+(["帮凶"] if n>=6 else [])+["良人"]*(n-(2 if n>=6 else 1))
    random.shuffle(roles)
    room.update(phase="investigate",round=1,targets=random.sample(TYPES,3),pending=[],
                reveal=[],history=[],winner=None,threshold=None)
    room["target"]=room["targets"][0]
    room["threshold"]=math.floor(roles.count("良人")/2)+1
    for i,(p,r) in enumerate(zip(room["players"],roles)):
        cards=[{"id":secrets.token_hex(6),"slot":j+1,"type":kind,"label":LABELS[kind][i]} for j,kind in enumerate(TYPES)]
        random.shuffle(cards)
        for j,c in enumerate(cards):c["slot"]=j+1
        p.update(role=r,hand=cards,done=False,peek=None,journal=[],vote=None)
    log(room,"司仪","灯火已暗。每位宾客的身份与线索均已密封送达。")
    advance(room)
def action(room,p,a):
    phase=room["phase"];w=a.get("action")
    host=p["id"]==room["host"]
    if w=="say":
        msg=str(a.get("text","")).strip()[:240]
        if not msg:reject("不能发送空消息")
        log(room,p["name"],msg)
    elif w=="ready" and phase=="lobby":
        p["ready"]=not p["ready"]
    elif w=="bot" and phase=="lobby" and host:
        if len(room["players"])>=MAX_PLAYERS:reject("房间最多 8 席")
        names={x["name"] for x in room["players"]}
        name=next((x for x in BOT_NAMES if x not in names),"AI 宾客")
        room["players"].append(person(name,True))
    elif w=="removeBot" and phase=="lobby" and host:
        x=next((x for x in room["players"] if x["id"]==a.get("playerId") and x["bot"]),None)
        if not x:reject("只能移除 AI")
        room["players"].remove(x)
    elif w=="start" and phase=="lobby" and host:start_game(room)
    elif w=="inspect" and phase=="investigate" and not p["done"]:
        other=next((x for x in room["players"] if x["id"]==a.get("playerId") and x!=p),None)
        if not other:reject("请选择另一位宾客")
        slot=a.get("slot")
        if not isinstance(slot,int):reject("请选择一张暗牌")
        c=next((c for c in other["hand"] if c["slot"]==slot),None)
        if not c:reject("这张牌已被提交，请重新选")
        glimpse={"round":room["round"],"name":other["name"],"slot":slot,"type":c["type"],"label":c["label"]}
        p["peek"]=glimpse;p["journal"].append(glimpse);p["done"]=True
    elif w=="submit" and phase=="submit" and not p["done"]:
        c=next((c for c in p["hand"] if c["id"]==a.get("cardId")),None)
        if not c:reject("手牌已变化，请重新选择")
        legal=(c["type"]!=room["target"]) if p["role"]=="真凶" else (c["type"]==room["target"])
        if not legal:reject("该牌不符合你的身份出牌规则")
        p["hand"].remove(c);room["pending"].append({"type":c["type"],"label":c["label"]});p["done"]=True
    elif w=="next" and phase=="reveal" and not p["done"]:p["done"]=True
    elif w=="vote" and phase=="vote" and not p["done"]:
        target=next((x for x in room["players"] if x["id"]==a.get("playerId") and x!=p),None)
        if not target:reject("请选择另一位宾客")
        p["vote"]=target["id"];p["done"]=True
    elif w=="reset" and phase=="end" and host:
        room.update(phase="lobby",round=0,target=None,reveal=[],history=[],pending=[],winner=None)
        for x in room["players"]:x.update(role=None,hand=[],ready=x["bot"],done=False,peek=None,journal=[],vote=None)
    elif w=="leave" and phase=="lobby":
        room["players"].remove(p)
        SOCKETS.pop(p["id"],None)
        if not room["players"]:ROOMS.pop(room["code"],None)
        elif host:room["host"]=next((x["id"] for x in room["players"] if not x["bot"]),room["players"][0]["id"])
    else:reject("当前阶段或权限不允许操作")
    room["updated"]=now()
    if room["code"] in ROOMS:advance(room)
    save()
async def root(req):
    resp=web.FileResponse(Path(__file__).with_name("multiplayer-v3.html"))
    resp.headers["Cache-Control"]="no-store"
    return resp
async def health(req):return web.json_response({"status":"ok","version":3,"rooms":len(ROOMS)})
async def create(req):
    try:
        data=await req.json()
        if not isinstance(data,dict):reject("JSON格式错误")
        if len(ROOMS)>=MAX_ROOMS:reject("当前房间过多")
        name=safe_name(data.get("name"))
        c=new_code()
        while c in ROOMS:c=new_code()
        p=person(name)
        ROOMS[c]={"code":c,"phase":"lobby","round":0,"target":None,"host":p["id"],
            "players":[p],"targets":[],"pending":[],"reveal":[],"history":[],"chat":[],
            "winner":None,"threshold":0,"updated":now()}
        save()
        return web.json_response({"code":c,"token":p["token"],"playerId":p["id"]})
    except (ValueError,TypeError):reject("请求格式错误")
async def join(req):
    try:
        data=await req.json();room=clean_room(data.get("code"))
        if room["phase"]!="lobby":reject("游戏已经开始，请让房主新开一局")
        if len(room["players"])>=MAX_PLAYERS:reject("房间已满")
        name=safe_name(data.get("name"))
        if any(x["name"].lower()==name.lower() for x in room["players"]):reject("昵称已占用")
        p=person(name);room["players"].append(p);room["updated"]=now();save()
        await broadcast(room)
        return web.json_response({"code":room["code"],"token":p["token"],"playerId":p["id"]})
    except (ValueError,TypeError):reject("请求格式错误")
async def ws_handler(req):
    ws=web.WebSocketResponse(heartbeat=20,max_msg_size=4096)
    await ws.prepare(req)
    room=None;p=None
    try:
        async for m in ws:
            if m.type!=WSMsgType.TEXT:continue
            try:
                data=json.loads(m.data)
                if not isinstance(data,dict):reject("消息格式错误")
                if p is None:
                    if data.get("type")!="hello":reject("请先认证")
                    room=clean_room(data.get("code"))
                    p=get_user(room,data.get("token"))
                    if not p:reject("登录凭证已过期，请重新加入")
                    old=SOCKETS.get(p["id"])
                    SOCKETS[p["id"]]=ws;p["offline_since"]=None
                    if old and old!=ws and not old.closed:await old.close()
                    await ws.send_json({"type":"welcome","room":view(room,p)})
                else:
                    if data.get("type")!="action":reject("未知操作")
                    payload=data.get("payload")
                    if not isinstance(payload,dict):reject("参数格式无效")
                    action(room,p,payload)
                    await ws.send_json({"type":"ack"})
                if room and room["code"] in ROOMS:await broadcast(room)
            except web.HTTPException as e:
                try:msg=json.loads(e.text).get("error","操作失败")
                except Exception:msg="操作失败"
                await ws.send_json({"type":"error","message":msg})
            except Exception as exc:
                print("ws error:",type(exc).__name__,str(exc)[:200],flush=True)
                await ws.send_json({"type":"error","message":"请求处理失败"})
    finally:
        if p and SOCKETS.get(p["id"]) is ws:
            SOCKETS.pop(p["id"],None);p["offline_since"]=now();save()
            if room and room["code"] in ROOMS:await broadcast(room)
    return ws
async def sweep(app):
    while True:
        await asyncio.sleep(3)
        changed=[]
        for code,room in list(ROOMS.items()):
            if now()-room.get("updated",now())> (3*3600 if room["phase"]=="end" else 5*3600):
                ROOMS.pop(code,None);continue
            host=next((p for p in room["players"] if p["id"]==room["host"]),None)
            if host and not connected(host) and host["offline_since"] and now()-host["offline_since"]>GRACE:
                replacement=next((x for x in room["players"] if not x["bot"] and connected(x)),None)
                if replacement:room["host"]=replacement["id"];changed.append(room)
            old=(room["phase"],room["round"],sum(p["done"] for p in room["players"]))
            advance(room)
            if old!=(room["phase"],room["round"],sum(p["done"] for p in room["players"])):changed.append(room)
        if changed:save()
        for room in changed:await broadcast(room)
async def app_startup(app):app["ticker"]=asyncio.create_task(sweep(app))
async def app_cleanup(app):
    app["ticker"].cancel()
    try:await app["ticker"]
    except asyncio.CancelledError:pass
    save()
app.router.add_get("/",root)
app.router.add_get("/health",health)
app.router.add_post("/api/rooms",create)
app.router.add_post("/api/join",join)
app.router.add_get("/ws",ws_handler)
app.on_startup.append(app_startup)
app.on_cleanup.append(app_cleanup)
if __name__=="__main__":
    web.run_app(app,host="0.0.0.0",port=int(os.environ.get("PORT","8765")))
