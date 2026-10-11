import sqlite3,secrets,hashlib,os,json,fitz,time,re,random,datetime,urllib.request,urllib.error,urllib.parse,threading,zipfile,io,collections
from fastapi import FastAPI,Depends,HTTPException,UploadFile,File,Form,Header,Body,WebSocket
from fastapi.responses import FileResponse,Response,JSONResponse
from pydantic import BaseModel
os.makedirs("files",exist_ok=True)
db=sqlite3.connect("okul.db",check_same_thread=False)
db.row_factory=sqlite3.Row
db.executescript("""
create table if not exists users(id integer primary key,name text,email text unique,pw text,salt text,role text);
create table if not exists sessions(token text primary key,user_id integer);
create table if not exists books(id integer primary key,owner_id integer,title text,pages integer,shared integer default 0);
create table if not exists texts(book_id integer,page integer,text text);
create table if not exists notes(user_id integer,book_id integer,page integer,data text,primary key(user_id,book_id,page));
create table if not exists assignments(id integer primary key,teacher_id integer,title text,description text,book_id integer,due text);
create table if not exists submissions(id integer primary key,assignment_id integer,student_id integer,book_id integer,text text,status text default 'teslim',feedback text default '',grade text default '',unique(assignment_id,student_id));
create table if not exists lessons(id integer primary key,teacher_id integer,title text,active integer default 1,code text);
create table if not exists lesson_members(lesson_id integer,user_id integer,primary key(lesson_id,user_id));
create table if not exists classes(id integer primary key,teacher_id integer,name text,code text);
create table if not exists class_members(class_id integer,user_id integer,primary key(class_id,user_id));
create table if not exists class_roster(id integer primary key,class_id integer,name text,email text,unique(class_id,email));
create table if not exists lesson_pages(lesson_id integer,book_id integer,page integer,at real,primary key(lesson_id,book_id,page));
create table if not exists attendance(lesson_id integer,user_id integer,joins integer default 0,secs integer default 0,hands integer default 0,chats integer default 0,primary key(lesson_id,user_id));
create table if not exists settings(user_id integer primary key,provider text default 'gemini',gemini_key text default '',gemini_model text default '',or_key text default '',or_model text default '',share integer default 0);
create table if not exists ai_log(user_id integer,at real);
create table if not exists exams(id integer primary key,owner_id integer,class_id integer,title text,kind text default 'sinav',scope text default 'class',start_at real,end_at real,duration integer default 0,guests integer default 0,shuffle integer default 1,code text,created real,source text default '');
create table if not exists exam_members(exam_id integer,user_id integer,primary key(exam_id,user_id));
create table if not exists questions(id integer primary key,exam_id integer,idx integer,q text,opts text,ans integer,expl text default '',topic text default '');
create table if not exists attempts(id integer primary key,exam_id integer,user_id integer,started real,finished real,deadline real,answers text default '{}',ord text default '[]',correct integer default 0,wrong integer default 0,blank integer default 0,score real default 0,switches integer default 0,away_sec integer default 0,copies integer default 0,away_since real,events text default '[]',status text default 'devam');
create table if not exists grades(id integer primary key,class_id integer,student_id integer,teacher_id integer,kind text,score real,note text default '',at real,exam_id integer);
""")
for col in("code text","class_id integer","guests integer default 0","started_at real","ended_at real","topic text default ''"):
    try:
        db.execute("alter table lessons add column "+col)
    except sqlite3.OperationalError:
        pass
for stmt in ("alter table sessions add column created real","alter table class_members add column active integer default 1","alter table exams add column private integer default 0","alter table users add column is_admin integer default 0"):
    try:
        db.execute(stmt)
    except sqlite3.OperationalError:
        pass
db.commit()
db.execute("create table if not exists notebook(user_id integer,lesson_id integer,page integer,data text,primary key(user_id,lesson_id,page))")
db.execute("create table if not exists question_pool(id integer primary key,teacher_id integer,topic text,q text,opts text,ans integer,expl text default '',created real)")
db.execute("create table if not exists parent_links(parent_id integer,student_id integer,primary key(parent_id,student_id))")
db.execute("create table if not exists parent_codes(code text primary key,student_id integer,exp real)")
db.execute("create table if not exists announcements(id integer primary key,teacher_id integer,class_id integer,title text,body text,created real)")
RECORD_HOURS=3
RECORD_LIMIT=1024*1024*1024
os.makedirs("recordings",exist_ok=True)
db.execute("create table if not exists recordings(id integer primary key,lesson_id integer,teacher_id integer,class_id integer,path text,created real,expires real,size integer)")
db.execute("create table if not exists goals(user_id integer primary key,target real)")
db.execute("create table if not exists audit(id integer primary key,at real,user_id integer,user_name text,action text,detail text)")
db.execute("create table if not exists roll(lesson_id integer,user_id integer,at real,primary key(lesson_id,user_id))")
for stmt in ("alter table roll add column status text default 'var'","alter table roll add column early integer default 0"):
    try:
        db.execute(stmt)
    except sqlite3.OperationalError:
        pass
db.commit()
db.execute("create table if not exists resets(email text primary key,code text,exp real,tries integer default 0)")
db.commit()
app=FastAPI()
BACKUP_KEEP=14
def backup_once():
    os.makedirs("backups",exist_ok=True)
    stamp=time.strftime("%Y%m%d")
    path=f"backups/okul-{stamp}.zip"
    if os.path.exists(path):
        return
    snap=sqlite3.connect(":memory:")
    with DBLOCK:
        db.backup(snap)
    tmp=path+".tmp"
    with zipfile.ZipFile(tmp,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("okul.db",snap.serialize())
        for root,_,names in os.walk("files"):
            for n in names:
                p=os.path.join(root,n)
                z.write(p,p.replace("\\","/"))
    os.replace(tmp,path)
    olds=sorted(f for f in os.listdir("backups") if f.startswith("okul-") and f.endswith(".zip"))
    for f in olds[:-BACKUP_KEEP]:
        os.remove(os.path.join("backups",f))
    extra=os.environ.get("YEDEK_KLASORU")
    if extra and os.path.isdir(extra):
        import shutil
        shutil.copy2(path,os.path.join(extra,os.path.basename(path)))
def backup_loop():
    while True:
        try:
            backup_once()
        except Exception:
            pass
        time.sleep(3600)
threading.Thread(target=backup_loop,daemon=True).start()
def purge_recordings():
    now=time.time()
    for r in q("select id,path from recordings where expires<?",(now,)).fetchall():
        try:
            os.remove(r["path"])
        except OSError:
            pass
        q("delete from recordings where id=?",(r["id"],))
def rec_loop():
    while True:
        try:
            purge_recordings()
        except Exception:
            pass
        time.sleep(300)
threading.Thread(target=rec_loop,daemon=True).start()
JOBS={}
RSALT="sifirla".encode().hex()
rooms={}
DBLOCK=threading.RLock()
class QResult:
    def __init__(self,c):
        self.rows=c.fetchall() if c.description else []
        self.rowcount=c.rowcount
        self.lastrowid=c.lastrowid
    def fetchone(self):
        return self.rows[0] if self.rows else None
    def fetchall(self):
        return self.rows
    def __iter__(self):
        return iter(self.rows)
def q(sql,a=()):
    with DBLOCK:
        c=db.execute(sql,a)
        res=QResult(c)
        db.commit()
    return res
def hp(p,s):
    return hashlib.pbkdf2_hmac("sha256",p.encode(),bytes.fromhex(s),100000).hex()
def pub(u):
    return {"id":u["id"],"name":u["name"],"role":u["role"],"admin":bool(u["is_admin"]) if "is_admin" in u.keys() else False}
SESSION_DAYS=30
def start(uid):
    t=secrets.token_hex(32)
    q("delete from sessions where created<? or created is null",(time.time()-SESSION_DAYS*86400,))
    q("insert into sessions(token,user_id,created) values(?,?,?)",(t,uid,time.time()))
    return t
def new_code():
    a="ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        c="".join(secrets.choice(a) for _ in range(6))
        if not any(q(f"select 1 from {t} where code=?",(c,)).fetchone() for t in("lessons","classes","exams")):
            return c
def by_token(t):
    return q("select u.* from users u join sessions s on s.user_id=u.id where s.token=? and s.created>?",(t,time.time()-SESSION_DAYS*86400)).fetchone()
@app.post("/api/logout")
def logout(authorization:str=Header(None)):
    q("delete from sessions where token=?",((authorization or "").split(" ")[-1],))
    return {"ok":True}
def me(authorization:str=Header(None)):
    r=by_token((authorization or "").split(" ")[-1])
    if not r:
        raise HTTPException(401,"Giriş gerekli")
    return r
def teacher(u=Depends(me)):
    if u["role"]!="teacher":
        raise HTTPException(403,"Sadece öğretmen")
    return u
def audit(u,action,detail=""):
    q("insert into audit(at,user_id,user_name,action,detail) values(?,?,?,?,?)",(time.time(),u["id"],u["name"],action,str(detail)[:300]))
def admin_dep(u=Depends(me)):
    if u["role"]!="teacher" or not u["is_admin"]:
        raise HTTPException(403,"Yalnızca yönetici")
    return u
def can(bid,u):
    b=q("select * from books where id=?",(bid,)).fetchone()
    if not b or not(b["owner_id"]==u["id"] or b["shared"] or u["role"]=="teacher" or q("select 1 from lesson_pages p join lessons l on l.id=p.lesson_id where p.book_id=? and (l.class_id in(select class_id from class_members where user_id=?) or l.id in(select lesson_id from lesson_members where user_id=?))",(bid,u["id"],u["id"])).fetchone()):
        raise HTTPException(404,"Kitap bulunamadı")
    return b
class Reg(BaseModel):
    name:str
    email:str
    password:str
    role:str="student"
    code:str=""
class Log(BaseModel):
    email:str
    password:str
class Asg(BaseModel):
    title:str
    description:str=""
    book_id:int|None=None
    due:str=""
class Sub(BaseModel):
    book_id:int|None=None
    text:str=""
class Fb(BaseModel):
    feedback:str=""
    grade:str=""
class Les(BaseModel):
    title:str
    class_id:int|None=None
    guests:int=0
class Code(BaseModel):
    code:str
class Forgot(BaseModel):
    email:str
class Reset(BaseModel):
    email:str
    code:str
    password:str
@app.post("/api/register")
def register(r:Reg):
    if len(r.password)<6:
        raise HTTPException(400,"Şifre en az 6 karakter")
    if q("select 1 from users where email=?",(r.email.lower(),)).fetchone():
        raise HTTPException(400,"Bu e-posta kayıtlı")
    s=secrets.token_hex(16)
    pc=None
    if r.role=="parent":
        pc=q("select student_id,exp from parent_codes where code=?",(r.code.strip().upper(),)).fetchone()
        if not pc or pc["exp"]<time.time():
            raise HTTPException(400,"Veli kodu geçersiz ya da süresi dolmuş")
        role="parent"
    else:
        role=r.role if r.role in("student","teacher") else "student"
    uid=q("insert into users(name,email,pw,salt,role) values(?,?,?,?,?)",(r.name,r.email.lower(),hp(r.password,s),s,role)).lastrowid
    if role=="teacher" and not q("select 1 from users where role='teacher' and id!=?",(uid,)).fetchone():
        q("update users set is_admin=1 where id=?",(uid,))
    if pc:
        q("insert or ignore into parent_links values(?,?)",(uid,pc["student_id"]))
        q("delete from parent_codes where code=?",(r.code.strip().upper(),))
    u=q("select * from users where id=?",(uid,)).fetchone()
    link_roster(u)
    return {"token":start(uid),"user":pub(u)}
FAILS={}
class Pw(BaseModel):
    old:str
    new:str
@app.middleware("http")
async def parent_guard(request,call_next):
    auth=request.headers.get("authorization","")
    tok=auth.split(" ")[-1] if auth else ""
    if tok and request.url.path.startswith("/api"):
        r=q("select u.role from users u join sessions s on s.user_id=u.id where s.token=?",(tok,)).fetchone()
        if r and r["role"]=="parent":
            p=request.url.path
            ok=p.startswith("/api/parent/") or p in ("/api/me","/api/password","/api/settings","/api/login","/api/register")
            if not ok:
                return JSONResponse({"detail":"Veli hesabı bu alana erişemez"},status_code=403)
    return await call_next(request)
class Promo(BaseModel):
    email:str
@app.get("/api/admin/overview")
def admin_overview(u=Depends(admin_dep)):
    teachers=[dict(r) for r in q("select t.id,t.name,t.email,t.is_admin,(select count(*) from classes c where c.teacher_id=t.id) classes,(select count(*) from class_members m join classes c on c.id=m.class_id where c.teacher_id=t.id) students,(select count(*) from exams e where e.owner_id=t.id) exams,(select count(*) from books b where b.owner_id=t.id) books from users t where t.role='teacher' order by t.name").fetchall()]
    totals={"teachers":len(teachers),"students":q("select count(*) n from users where role='student'").fetchone()["n"],"classes":q("select count(*) n from classes").fetchone()["n"],"books":q("select count(*) n from books").fetchone()["n"],"exams":q("select count(*) n from exams").fetchone()["n"]}
    classes=[dict(r) for r in q("select c.id,c.name,t.name teacher,(select count(*) from class_members m where m.class_id=c.id) students from classes c join users t on t.id=c.teacher_id order by t.name,c.name").fetchall()]
    backups=sorted(f for f in os.listdir("backups") if f.startswith("okul-")) if os.path.isdir("backups") else []
    return {"totals":totals,"teachers":teachers,"classes":classes,"backups":backups[-5:],"backup_count":len(backups)}
@app.get("/api/lessons/{lid}/roll")
def roll_list(lid:int,u=Depends(teacher)):
    l=q("select class_id from lessons where id=? and teacher_id=?",(lid,u["id"])).fetchone()
    if not l:
        raise HTTPException(404,"Ders bulunamadı")
    present=[dict(r) for r in q("select u.id,u.name,r.status,r.early from roll r join users u on u.id=r.user_id where r.lesson_id=? order by u.name",(lid,)).fetchall()]
    ids={p["id"] for p in present}
    absent=[]
    if l["class_id"]:
        for r in q("select u.id,u.name from class_members m join users u on u.id=m.user_id where m.class_id=? order by u.name",(l["class_id"],)).fetchall():
            if r["id"] not in ids:
                absent.append({"id":r["id"],"name":r["name"]})
    return {"present":present,"absent":absent}
@app.get("/api/lessons/{lid}/summary")
def lesson_summary(lid:int,u=Depends(teacher)):
    l=q("select * from lessons where id=? and teacher_id=?",(lid,u["id"])).fetchone()
    if not l:
        raise HTTPException(404,"Ders bulunamadı")
    att=[dict(r) for r in q("select u.name,a.joins,a.secs,a.hands,a.chats from attendance a join users u on u.id=a.user_id where a.lesson_id=? order by u.name",(lid,)).fetchall()]
    pages=[dict(r) for r in q("select b.title,p.page from lesson_pages p join books b on b.id=p.book_id where p.lesson_id=? order by p.at",(lid,)).fetchall()]
    return {"title":l["title"],"topic":l["topic"],"attendance":att,"pages":pages}
class Goal(BaseModel):
    target:float
@app.get("/api/me/goal")
def get_goal(u=Depends(me)):
    r=q("select target from goals where user_id=?",(u["id"],)).fetchone()
    return {"target":r["target"] if r else None}
@app.put("/api/me/goal")
def set_goal(g:Goal,u=Depends(me)):
    if u["role"]!="student":
        raise HTTPException(403,"Yalnızca öğrenciler hedef koyabilir")
    if not 0<=g.target<=100:
        raise HTTPException(400,"Hedef 0 ile 100 arasında olmalı")
    q("insert or replace into goals(user_id,target) values(?,?)",(u["id"],g.target))
    return {"ok":True}
@app.get("/api/exams/{eid}/pairs")
def exam_pairs(eid:int,u=Depends(teacher)):
    e=q("select id,title from exams where id=? and owner_id=?",(eid,u["id"])).fetchone()
    if not e:
        raise HTTPException(404,"Sınav bulunamadı")
    qs={x["id"]:x["ans"] for x in q("select id,ans from questions where exam_id=?",(eid,)).fetchall()}
    rows=q("select a.user_id,u.name,a.answers from attempts a join users u on u.id=a.user_id where a.exam_id=? and a.status='bitti'",(eid,)).fetchall()
    subs=[]
    for r in rows:
        ans={int(k):v for k,v in json.loads(r["answers"] or "{}").items() if k.isdigit()}
        wrong={qid:v for qid,v in ans.items() if qid in qs and v!=qs[qid]}
        subs.append((r["user_id"],r["name"],wrong))
    out=[]
    for i in range(len(subs)):
        for j in range(i+1,len(subs)):
            ua,na,wa=subs[i]
            ub,nb,wb=subs[j]
            same=sum(1 for k in wa if k in wb and wa[k]==wb[k])
            if same>=3:
                ratio=round(same/max(1,min(len(wa),len(wb))),2)
                out.append({"a":na,"b":nb,"same":same,"ratio":ratio})
    out.sort(key=lambda x:(-x["ratio"],-x["same"]))
    return {"title":e["title"],"pairs":out[:15]}
@app.get("/api/admin/conflicts")
def conflicts(u=Depends(admin_dep)):
    rows=q("select e.title,e.start_at,e.end_at,e.class_id,c.name cname from exams e join classes c on c.id=e.class_id where e.scope='class' and e.private=0 order by e.class_id,e.start_at").fetchall()
    out=[]
    for i,r in enumerate(rows):
        for s in rows[i+1:]:
            if s["class_id"]==r["class_id"] and s["start_at"]<r["end_at"] and r["start_at"]<s["end_at"]:
                out.append(f"{r['cname']}: '{r['title']}' ile '{s['title']}' zaman olarak çakışıyor")
    days={}
    for r in rows:
        d=time.strftime("%Y-%m-%d",time.localtime(r["start_at"]))
        days.setdefault((r["class_id"],d),[]).append(r)
    for (cid,d),lst in days.items():
        if len(lst)>=2:
            out.append(f"{lst[0]['cname']}: {d} tarihinde {len(lst)} sınav var")
    return out
def can_rec(r,u):
    if u["role"]=="teacher" and u["id"]==r["teacher_id"]:
        return True
    if not r["class_id"]:
        return False
    return bool(q("select 1 from class_members where class_id=? and user_id=?",(r["class_id"],u["id"])).fetchone())
@app.post("/api/lessons/{lid}/recordings")
async def upload_rec(lid:int,file:UploadFile=File(...),consent:int=Form(0),u=Depends(teacher)):
    l=q("select id,class_id from lessons where id=? and teacher_id=?",(lid,u["id"])).fetchone()
    if not l:
        raise HTTPException(404,"Ders bulunamadı")
    if not consent:
        raise HTTPException(400,"Veli onayı onaylanmadan kayıt saklanamaz")
    now=time.time()
    path=f"recordings/{secrets.token_hex(8)}.webm"
    size=0
    try:
        with open(path,"wb") as fh:
            while chunk:=await file.read(1<<20):
                size+=len(chunk)
                if size>RECORD_LIMIT:
                    raise ValueError("too big")
                fh.write(chunk)
    except ValueError:
        os.remove(path)
        raise HTTPException(400,"Kayıt çok büyük")
    q("insert into recordings(lesson_id,teacher_id,class_id,path,created,expires,size) values(?,?,?,?,?,?,?)",(lid,u["id"],l["class_id"],path,now,now+RECORD_HOURS*3600,size))
    audit(u,"ders kaydı",f"ders {lid} ({size} bayt, {RECORD_HOURS} saat saklanır)")
    return {"ok":True,"expires":now+RECORD_HOURS*3600}
@app.get("/api/recordings")
def rec_list(u=Depends(me)):
    now=time.time()
    if u["role"]=="teacher":
        rows=q("select r.id,r.created,r.expires,l.title from recordings r join lessons l on l.id=r.lesson_id where r.teacher_id=? and r.expires>? order by r.created desc",(u["id"],now)).fetchall()
    else:
        rows=q("select r.id,r.created,r.expires,l.title from recordings r join lessons l on l.id=r.lesson_id where r.expires>? and r.class_id in(select class_id from class_members where user_id=?) order by r.created desc",(now,u["id"])).fetchall()
    return [dict(x) for x in rows]
@app.get("/api/recordings/{rid}/file")
def rec_file(rid:int,u=Depends(me)):
    r=q("select * from recordings where id=?",(rid,)).fetchone()
    if not r or r["expires"]<time.time() or not can_rec(r,u):
        raise HTTPException(404,"Kayıt bulunamadı ya da süresi doldu")
    return FileResponse(r["path"],media_type="video/webm")
@app.get("/api/admin/audit")
def audit_list(u=Depends(admin_dep)):
    return [dict(r) for r in q("select at,user_name,action,detail from audit order by id desc limit 200").fetchall()]
@app.post("/api/admin/promote")
def promote(p:Promo,u=Depends(admin_dep)):
    if not q("update users set is_admin=1 where email=? and role='teacher'",(p.email.strip().lower(),)).rowcount:
        raise HTTPException(404,"Bu e-posta ile kayıtlı öğretmen yok")
    audit(u,"yönetici yapma",p.email)
    return {"ok":True}
@app.get("/api/notify")
def notify(since:float=0,u=Depends(me)):
    if u["role"]=="teacher":
        return []
    now=time.time()
    ev=[{"kind":"ann","title":f"📢 {a['title']} ({a['cname']})"} for a in q("select a.title,c.name cname from announcements a join classes c on c.id=a.class_id where a.created>? and a.class_id in(select class_id from class_members where user_id=?) order by a.created",(since,u["id"])).fetchall()]
    ev+=[{"kind":"exam","title":f"🧪 Sınav açıldı: {x['title']}"} for x in q("select e.title from exams e where e.scope='class' and e.start_at>? and e.start_at<=? and ((e.private=0 and e.class_id in(select class_id from class_members where user_id=?)) or e.id in(select exam_id from exam_members where user_id=?))",(since,now,u["id"],u["id"])).fetchall()]
    return ev[:20]
@app.post("/api/me/parent-code")
def parent_code(u=Depends(me)):
    if u["role"]!="student":
        raise HTTPException(403,"Yalnızca öğrenciler veli kodu üretebilir")
    code=secrets.token_hex(3).upper()
    q("insert into parent_codes values(?,?,?)",(code,u["id"],time.time()+7*86400))
    return {"code":code,"days":7}
@app.get("/api/parent/children")
def my_children(u=Depends(me)):
    if u["role"]!="parent":
        raise HTTPException(403,"Yalnızca veliler")
    return [dict(r) for r in q("select x.id,x.name from parent_links l join users x on x.id=l.student_id where l.parent_id=? order by x.name",(u["id"],)).fetchall()]
@app.get("/api/parent/children/{sid}/progress")
def child_progress(sid:int,u=Depends(me)):
    if u["role"]!="parent" or not q("select 1 from parent_links where parent_id=? and student_id=?",(u["id"],sid)).fetchone():
        raise HTTPException(404,"Öğrenci bulunamadı")
    c=q("select class_id from class_members where user_id=? limit 1",(sid,)).fetchone()
    if not c:
        raise HTTPException(404,"Öğrenci henüz bir sınıfa bağlı değil")
    return profile_for(c["class_id"],sid)
@app.post("/api/login")
def login(r:Log):
    em=r.email.lower().strip()
    now=time.time()
    recent=[t for t in FAILS.get(em,[]) if now-t<900]
    FAILS[em]=recent
    if len(recent)>=8:
        raise HTTPException(429,"Çok fazla hatalı deneme. 15 dakika sonra tekrar dene")
    u=q("select * from users where email=?",(em,)).fetchone()
    if not u or hp(r.password,u["salt"])!=u["pw"]:
        FAILS[em].append(now)
        raise HTTPException(401,"E-posta veya şifre hatalı")
    FAILS.pop(em,None)
    link_roster(u)
    return {"token":start(u["id"]),"user":pub(u)}
@app.post("/api/forgot")
def forgot(r:Forgot):
    em=r.email.strip().lower()
    if q("select 1 from users where email=?",(em,)).fetchone():
        code=f"{secrets.randbelow(900000)+100000}"
        q("insert or replace into resets(email,code,exp,tries) values(?,?,?,0)",(em,hp(code,RSALT),time.time()+900))
        print(f"\n[ŞİFRE SIFIRLAMA] {em} için doğrulama kodu: {code} (15 dakika geçerli)\n",flush=True)
    return {"ok":True}
@app.post("/api/reset")
def reset(r:Reset):
    em=r.email.strip().lower()
    row=q("select code,exp,tries from resets where email=?",(em,)).fetchone()
    if not row or row["exp"]<time.time():
        raise HTTPException(400,"Kod geçersiz veya süresi dolmuş, yeni kod iste")
    if hp(r.code.strip(),RSALT)!=row["code"]:
        t=row["tries"]+1
        if t>=5:
            q("delete from resets where email=?",(em,))
        else:
            q("update resets set tries=? where email=?",(t,em))
        raise HTTPException(400,"Kod hatalı")
    if len(r.password)<6:
        raise HTTPException(400,"Şifre en az 6 karakter")
    u=q("select id from users where email=?",(em,)).fetchone()
    s=secrets.token_hex(16)
    q("update users set pw=?,salt=? where email=?",(hp(r.password,s),s,em))
    q("delete from sessions where user_id=?",(u["id"],))
    q("delete from resets where email=?",(em,))
    return {"ok":True}
@app.post("/api/password")
def change_pw(p:Pw,u=Depends(me)):
    if hp(p.old,u["salt"])!=u["pw"]:
        raise HTTPException(400,"Mevcut şifre hatalı")
    if len(p.new)<6:
        raise HTTPException(400,"Yeni şifre en az 6 karakter")
    s=secrets.token_hex(16)
    q("update users set pw=?,salt=? where id=?",(hp(p.new,s),s,u["id"]))
    q("delete from sessions where user_id=?",(u["id"],))
    return {"token":start(u["id"])}
class Ann(BaseModel):
    title:str
    body:str=""
    class_id:int
@app.post("/api/announcements")
def add_ann(a:Ann,u=Depends(teacher)):
    if not a.title.strip():
        raise HTTPException(400,"Başlık gerekli")
    own_class(a.class_id,u)
    q("insert into announcements(teacher_id,class_id,title,body,created) values(?,?,?,?,?)",(u["id"],a.class_id,a.title.strip()[:120],a.body.strip()[:2000],time.time()))
    return {"ok":True}
@app.get("/api/announcements")
def list_ann(u=Depends(me)):
    if u["role"]=="teacher":
        rows=q("select a.*,c.name cname from announcements a join classes c on c.id=a.class_id where a.teacher_id=? order by a.created desc limit 50",(u["id"],)).fetchall()
    else:
        rows=q("select a.*,c.name cname,t.name teacher from announcements a join classes c on c.id=a.class_id join users t on t.id=a.teacher_id where a.class_id in(select class_id from class_members where user_id=?) order by a.created desc limit 50",(u["id"],)).fetchall()
    return [dict(r) for r in rows]
@app.delete("/api/announcements/{aid}")
def del_ann(aid:int,u=Depends(teacher)):
    q("delete from announcements where id=? and teacher_id=?",(aid,u["id"]))
    return {"ok":True}
@app.get("/api/backup")
def backup(u=Depends(teacher)):
    snap=sqlite3.connect(":memory:")
    with DBLOCK:
        db.backup(snap)
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("okul.db",snap.serialize())
        for root,_,names in os.walk("files"):
            for n in names:
                p=os.path.join(root,n)
                z.write(p,p.replace("\\","/"))
    return Response(buf.getvalue(),media_type="application/zip",headers={"Content-Disposition":'attachment; filename="dijital-okul-yedek.zip"'})
@app.get("/api/me")
def whoami(u=Depends(me)):
    return pub(u)
@app.post("/api/books")
async def add_book(file:UploadFile=File(...),title:str=Form(""),shared:int=Form(0),u=Depends(me)):
    if u["role"]=="guest":
        raise HTTPException(403,"Misafirler kitap yükleyemez")
    data=await file.read()
    try:
        doc=fitz.open(stream=data,filetype="pdf")
    except Exception:
        raise HTTPException(400,"Geçersiz PDF")
    bid=q("insert into books(owner_id,title,pages,shared) values(?,?,?,?)",(u["id"],title or file.filename,len(doc),1 if shared and u["role"]=="teacher" else 0)).lastrowid
    open(f"files/{bid}.pdf","wb").write(data)
    for i,p in enumerate(doc,1):
        t=p.get_text()
        if not t.strip():
            try:
                t=p.get_textpage_ocr(full=True).extractText()
            except Exception:
                t=""
        q("insert into texts values(?,?,?)",(bid,i,t))
    db.commit()
    return {"id":bid}
@app.post("/api/books/images")
async def add_book_images(files:list[UploadFile]=File(...),title:str=Form(""),shared:int=Form(0),u=Depends(me)):
    if u["role"]=="guest":
        raise HTTPException(403,"Misafirler kitap yükleyemez")
    if not 1<=len(files)<=300:
        raise HTTPException(400,"1-300 arası resim seçmelisin")
    out=fitz.open()
    for f in files:
        data=await f.read()
        try:
            im=fitz.open(stream=data)
            out.insert_pdf(fitz.open(stream=im.convert_to_pdf(),filetype="pdf"))
        except Exception:
            raise HTTPException(400,f"{f.filename} geçerli bir resim değil")
    name=(title.strip() or (files[0].filename or "Kitap").rsplit(".",1)[0])[:120]
    pdf=out.tobytes(deflate=True,garbage=3)
    bid=q("insert into books(owner_id,title,pages,shared) values(?,?,?,?)",(u["id"],name,len(out),1 if shared and u["role"]=="teacher" else 0)).lastrowid
    open(f"files/{bid}.pdf","wb").write(pdf)
    doc=fitz.open(stream=pdf,filetype="pdf")
    for i,p in enumerate(doc,1):
        t=""
        try:
            t=p.get_textpage_ocr(full=True).extractText()
        except Exception:
            t=""
        q("insert into texts values(?,?,?)",(bid,i,t))
    db.commit()
    return {"id":bid,"pages":len(doc)}
VSamp=collections.namedtuple("VSamp","g sharp idx")
def pick_pages(S):
    import numpy as np,cv2
    d=lambda a,b_: float(np.mean(cv2.absdiff(a,b_)))
    runs=[];start=0
    for i in range(1,len(S)+1):
        if i==len(S) or d(S[i].g,S[i-1].g)>=3:
            runs.append(S[start:i]);start=i
    pages=[]
    for seg in runs:
        best=max(seg,key=lambda s:s.sharp)
        if not pages or d(best.g,pages[-1].g)>=8:
            pages.append(best)
    return pages
def video_scan(path):
    import cv2
    cap=cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ValueError("Video açılamadı (biçim desteklenmiyor olabilir)")
    fps=cap.get(cv2.CAP_PROP_FPS) or 25
    step=max(1,int(round(fps*0.5)))
    S=[];idx=0
    while cap.grab():
        if idx%step==0:
            ok,f=cap.retrieve()
            if ok:
                h,w=f.shape[:2]
                c=f[int(h*.1):int(h*.9),int(w*.2):int(w*.8)]
                g=cv2.cvtColor(cv2.resize(c,(320,200)),cv2.COLOR_BGR2GRAY)
                sharp=float(cv2.Laplacian(cv2.cvtColor(cv2.resize(c,(640,400)),cv2.COLOR_BGR2GRAY),cv2.CV_64F).var())
                S.append(VSamp(g,sharp,idx))
        idx+=1
    cap.release()
    return pick_pages(S)
def page_crop(f):
    import cv2
    H,W=f.shape[:2]
    g=cv2.GaussianBlur(cv2.cvtColor(f,cv2.COLOR_BGR2GRAY),(5,5),0)
    _,m=cv2.threshold(g,170,255,cv2.THRESH_BINARY)
    cnts,_=cv2.findContours(m,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    cands=[c for c in cnts if cv2.boundingRect(c)[1]>2] or cnts
    if not cands:
        return f
    big=max(cands,key=cv2.contourArea)
    if cv2.contourArea(big)<0.08*H*W:
        return f
    x,y,w,hh=cv2.boundingRect(big)
    mg=int(0.01*max(w,hh))
    return f[max(0,y-mg):min(H,y+hh+mg),max(0,x-mg):min(W,x+w+mg)]
def page_images(f,split):
    import cv2
    c=page_crop(f)
    h,w=c.shape[:2]
    if h<0.35*f.shape[0]:
        return []
    parts=[c]
    if split and w/h>=1.2:
        parts=[c[:,:w//2],c[:,w//2:]]
    return [cv2.imencode(".jpg",p,[cv2.IMWRITE_JPEG_QUALITY,92])[1].tobytes() for p in parts]
def video_images(path,pages,split):
    import cv2
    want=[p.idx for p in pages];k=0;out=[]
    cap=cv2.VideoCapture(path);idx=0
    while k<len(want) and cap.grab():
        if idx==want[k]:
            ok,f=cap.retrieve()
            if ok:
                out.extend(page_images(f,split))
            k+=1
        idx+=1
    cap.release()
    return out
def video_job(jid,u,path,title,shared,split):
    try:
        pages=video_scan(path)
        if not pages:
            raise ValueError("Videoda sayfa algılanamadı. Kamera sabit olmalı ve her sayfa birkaç saniye durgun kalmalı")
        imgs=video_images(path,pages,split)
        out=fitz.open()
        for jpg in imgs:
            out.insert_pdf(fitz.open(stream=fitz.open(stream=jpg,filetype="jpg").convert_to_pdf(),filetype="pdf"))
        pdf=out.tobytes(deflate=True,garbage=3)
        bid=q("insert into books(owner_id,title,pages,shared) values(?,?,?,?)",(u["id"],title[:120],len(out),1 if shared and u["role"]=="teacher" else 0)).lastrowid
        open(f"files/{bid}.pdf","wb").write(pdf)
        for i,pg in enumerate(out,1):
            try:
                t=pg.get_textpage_ocr(full=True).extractText()
            except Exception:
                t=""
            q("insert into texts values(?,?,?)",(bid,i,t))
        db.commit()
        JOBS[jid]={"status":"done","pages":len(out),"book_id":bid,"uid":u["id"]}
    except ImportError:
        JOBS[jid]={"status":"error","error":"Video için OpenCV gerekli: python -m pip install opencv-python-headless numpy","uid":u["id"]}
    except ValueError as e:
        JOBS[jid]={"status":"error","error":str(e),"uid":u["id"]}
    except Exception as e:
        JOBS[jid]={"status":"error","error":"Videodan kitap oluşturulamadı: "+str(e)[:200],"uid":u["id"]}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
@app.post("/api/books/url")
def add_book_url(url:str=Form(...),title:str=Form(""),shared:int=Form(0),u=Depends(me)):
    if u["role"]=="guest":
        raise HTTPException(403,"Misafirler kitap yükleyemez")
    link=url.strip()
    if not link.lower().startswith(("https://","http://")):
        raise HTTPException(400,"Geçerli bir bağlantı gir (http:// ya da https:// ile başlamalı)")
    LIMIT=200*1024*1024
    try:
        req=urllib.request.Request(link,headers={"User-Agent":"DijitalOkul/1.0"})
        with urllib.request.urlopen(req,timeout=60) as r:
            data=r.read(LIMIT+1)
    except Exception:
        raise HTTPException(400,"Bağlantıdan dosya alınamadı")
    if len(data)>LIMIT:
        raise HTTPException(400,"Dosya çok büyük (en fazla 200 MB)")
    if not data[:5]==b"%PDF-":
        raise HTTPException(400,"Bağlantı bir PDF dosyası değil")
    try:
        doc=fitz.open(stream=data,filetype="pdf")
    except Exception:
        raise HTTPException(400,"PDF okunamadı")
    name=(title.strip() or os.path.basename(urllib.parse.urlparse(link).path).rsplit(".",1)[0] or "Kitap")[:120]
    bid=q("insert into books(owner_id,title,pages,shared) values(?,?,?,?)",(u["id"],name,len(doc),1 if shared and u["role"]=="teacher" else 0)).lastrowid
    open(f"files/{bid}.pdf","wb").write(data)
    for i,p in enumerate(doc,1):
        t=p.get_text()
        q("insert into texts values(?,?,?)",(bid,i,t))
    db.commit()
    return {"id":bid,"pages":len(doc)}
@app.post("/api/books/video")
async def book_from_video(file:UploadFile=File(...),title:str=Form(""),shared:int=Form(0),split:int=Form(1),u=Depends(me)):
    if u["role"]=="guest":
        raise HTTPException(403,"Misafirler kitap yükleyemez")
    jid=secrets.token_hex(8)
    ext=os.path.splitext(file.filename or "")[1].lower() or ".mp4"
    path=f"files/_v_{jid}{ext}"
    with open(path,"wb") as fh:
        while chunk:=await file.read(1<<20):
            fh.write(chunk)
    name=(title.strip() or os.path.splitext(file.filename or "Kitap")[0])[:120]
    JOBS[jid]={"status":"running","uid":u["id"]}
    threading.Thread(target=video_job,args=(jid,dict(u),path,name,shared,bool(split)),daemon=True).start()
    return {"job":jid}
@app.get("/api/books")
def books(search:str="",u=Depends(me)):
    if search:
        rows=q("select b.id,b.owner_id,b.title,b.pages,b.shared,t.page,substr(t.text,max(instr(lower(t.text),lower(?))-30,1),100) snip from books b join texts t on t.book_id=b.id where (b.owner_id=? or b.shared=1) and t.text like ? order by b.id desc limit 50",(search,u["id"],f"%{search}%")).fetchall()
    else:
        rows=q("select * from books where owner_id=? or shared=1 order by id desc",(u["id"],)).fetchall()
    return [dict(r) for r in rows]
@app.get("/api/books/{bid}/file")
def book_file(bid:int,u=Depends(me)):
    can(bid,u)
    return FileResponse(f"files/{bid}.pdf",media_type="application/pdf")
@app.get("/api/books/{bid}/cover")
def cover(bid:int,u=Depends(me)):
    can(bid,u)
    p=f"files/{bid}_c.png"
    if not os.path.exists(p):
        d=fitz.open(f"files/{bid}.pdf")
        d[0].get_pixmap(matrix=fitz.Matrix(.6,.6)).save(p)
    return FileResponse(p,media_type="image/png")
@app.delete("/api/books/{bid}")
def del_book(bid:int,u=Depends(me)):
    b=can(bid,u)
    if b["owner_id"]!=u["id"]:
        raise HTTPException(403,"Yetki yok")
    for t in("books","texts","notes"):
        q(f"delete from {t} where {'id' if t=='books' else 'book_id'}=?",(bid,))
    for f in(f"files/{bid}.pdf",f"files/{bid}_c.png"):
        if os.path.exists(f):
            os.remove(f)
    return {"ok":True}
def in_lesson(lid,u):
    l=q("select teacher_id,class_id from lessons where id=?",(lid,)).fetchone()
    return bool(l and(u["id"]==l["teacher_id"] or q("select 1 from lesson_members where lesson_id=? and user_id=?",(lid,u["id"])).fetchone() or(l["class_id"] and q("select 1 from class_members where class_id=? and user_id=?",(l["class_id"],u["id"])).fetchone())))
@app.get("/api/lessons/{lid}/notebook")
def get_nb(lid:int,u=Depends(me)):
    if not in_lesson(lid,u):
        raise HTTPException(403,"Bu derse erişimin yok")
    return {r["page"]:json.loads(r["data"]) for r in q("select page,data from notebook where user_id=? and lesson_id=?",(u["id"],lid)).fetchall()}
@app.put("/api/lessons/{lid}/notebook/{pg}")
def put_nb(lid:int,pg:int,strokes:list=Body(...),u=Depends(me)):
    if not in_lesson(lid,u):
        raise HTTPException(403,"Bu derse erişimin yok")
    q("insert or replace into notebook values(?,?,?,?)",(u["id"],lid,pg,json.dumps(strokes)))
    return {"ok":True}
@app.get("/api/notes/{bid}/{pg}")
def get_notes(bid:int,pg:int,user:int=0,u=Depends(me)):
    can(bid,u)
    uid=user if user and u["role"]=="teacher" else u["id"]
    r=q("select data from notes where user_id=? and book_id=? and page=?",(uid,bid,pg)).fetchone()
    return json.loads(r["data"]) if r else []
@app.put("/api/notes/{bid}/{pg}")
def put_notes(bid:int,pg:int,strokes:list=Body(...),u=Depends(me)):
    can(bid,u)
    q("insert or replace into notes values(?,?,?,?)",(u["id"],bid,pg,json.dumps(strokes)))
    return {"ok":True}
@app.post("/api/assignments")
def add_asg(a:Asg,u=Depends(teacher)):
    return {"id":q("insert into assignments(teacher_id,title,description,book_id,due) values(?,?,?,?,?)",(u["id"],a.title,a.description,a.book_id,a.due)).lastrowid}
@app.get("/api/assignments")
def asgs(u=Depends(me)):
    rows=q("select a.*,u.name teacher from assignments a join users u on u.id=a.teacher_id order by a.id desc").fetchall()
    return [dict(r) for r in rows]
def own_asg(aid,u):
    a=q("select * from assignments where id=? and teacher_id=?",(aid,u["id"])).fetchone()
    if not a:
        raise HTTPException(404,"Ödev bulunamadı")
    return a
@app.get("/api/assignments/{aid}/missing")
def asg_missing(aid:int,u=Depends(teacher)):
    own_asg(aid,u)
    rows=q("select distinct u.id,u.name,u.email from class_members m join classes c on c.id=m.class_id join users u on u.id=m.user_id where c.teacher_id=? and u.id not in(select student_id from submissions where assignment_id=?) order by u.name",(u["id"],aid)).fetchall()
    return [dict(r) for r in rows]
@app.post("/api/assignments/{aid}/remind")
def asg_remind(aid:int,u=Depends(teacher)):
    a=own_asg(aid,u)
    n=0
    for c in q("select id from classes where teacher_id=?",(u["id"],)).fetchall():
        miss=q("select 1 from class_members m where m.class_id=? and m.user_id not in(select student_id from submissions where assignment_id=?) limit 1",(c["id"],aid)).fetchone()
        if miss:
            body=("Son teslim: "+a["due"]) if a["due"] else "Lütfen ödevini zamanında teslim et."
            q("insert into announcements(teacher_id,class_id,title,body,created) values(?,?,?,?,?)",(u["id"],c["id"],("Ödev hatırlatması: "+a["title"])[:120],body,time.time()))
            n+=1
    return {"ok":True,"classes":n}
@app.post("/api/assignments/{aid}/submit")
def submit(aid:int,s:Sub,u=Depends(me)):
    if u["role"]!="student":
        raise HTTPException(403,"Sadece öğrenci")
    if not q("select 1 from assignments where id=?",(aid,)).fetchone():
        raise HTTPException(404,"Ödev yok")
    if s.book_id:
        can(s.book_id,u)
    q("insert into submissions(assignment_id,student_id,book_id,text) values(?,?,?,?) on conflict(assignment_id,student_id) do update set book_id=excluded.book_id,text=excluded.text,status='teslim'",(aid,u["id"],s.book_id,s.text))
    return {"ok":True}
@app.get("/api/submissions")
def subs(u=Depends(me)):
    if u["role"]=="teacher":
        rows=q("select s.*,us.name student,a.title from submissions s join users us on us.id=s.student_id join assignments a on a.id=s.assignment_id where a.teacher_id=? order by s.id desc",(u["id"],)).fetchall()
    else:
        rows=q("select s.*,a.title from submissions s join assignments a on a.id=s.assignment_id where s.student_id=? order by s.id desc",(u["id"],)).fetchall()
    return [dict(r) for r in rows]
@app.post("/api/submissions/{sid}/feedback")
def feedback(sid:int,f:Fb,u=Depends(teacher)):
    c=q("update submissions set feedback=?,grade=?,status='değerlendirildi' where id=? and assignment_id in(select id from assignments where teacher_id=?)",(f.feedback,f.grade,sid,u["id"]))
    if not c.rowcount:
        raise HTTPException(404,"Teslim bulunamadı")
    audit(u,"ödev değerlendirme",f"teslim {sid} not {f.grade}")
    return {"ok":True}
@app.post("/api/lessons")
def add_lesson(l:Les,u=Depends(teacher)):
    if l.class_id:
        own_class(l.class_id,u)
    c=new_code()
    i=q("insert into lessons(teacher_id,title,code,class_id,guests,started_at) values(?,?,?,?,?,?)",(u["id"],l.title,c,l.class_id,1 if l.guests else 0,time.time())).lastrowid
    return {"id":i,"code":c}
@app.post("/api/lessons/join")
def join_lesson(c:Code,u=Depends(me)):
    l=q("select * from lessons where code=? and active=1",(c.code.strip().upper(),)).fetchone()
    if not l:
        raise HTTPException(404,"Geçersiz veya bitmiş ders kodu")
    q("insert or ignore into lesson_members values(?,?)",(l["id"],u["id"]))
    return {"id":l["id"],"title":l["title"]}
@app.get("/api/lessons")
def lessons(u=Depends(teacher)):
    rows=q("select l.id,l.title,l.code,l.class_id,l.guests,l.topic,c.name cname from lessons l left join classes c on c.id=l.class_id where l.teacher_id=? and l.active=1 order by l.id desc",(u["id"],)).fetchall()
    return [dict(r)|{"online":len(rooms.get(r["id"],{}).get("peers",{}))} for r in rows]
def pcounts(p):
    return [sum(1 for v in p["votes"].values() if v==i) for i in range(len(p["opts"]))]
async def cast(r,m,skip=None):
    for k,p in list(r["peers"].items()):
        if k!=skip:
            try:
                await p["ws"].send_json(m)
            except Exception:
                pass
@app.post("/api/lessons/{lid}/end")
async def end_lesson(lid:int,u=Depends(teacher)):
    if not q("update lessons set active=0,ended_at=? where id=? and teacher_id=?",(time.time(),lid,u["id"])).rowcount:
        raise HTTPException(404,"Ders bulunamadı")
    r=rooms.pop(lid,None)
    if r:
        for p in r["peers"].values():
            if not p["host"]:
                q("update attendance set secs=secs+? where lesson_id=? and user_id=?",(int(time.time()-p["t0"]),lid,p["uid"]))
                p["t0"]=time.time()
        for p in list(r["peers"].values()):
            try:
                if p["host"]:
                    await p["ws"].send_json({"t":"ended"})
                else:
                    row=q("select secs from attendance where lesson_id=? and user_id=?",(lid,p["uid"])).fetchone()
                    await p["ws"].send_json({"t":"ended","secs":row["secs"] if row else 0})
                await p["ws"].close()
            except Exception:
                pass
    rows=q("select u.name,a.secs from attendance a join users u on u.id=a.user_id where a.lesson_id=? order by u.name",(lid,)).fetchall()
    return {"ok":True,"summary":[{"name":x["name"],"secs":x["secs"]} for x in rows]}
@app.websocket("/ws/live/{lid}")
async def live(ws:WebSocket,lid:int,token:str=""):
    u=by_token(token)
    l=q("select * from lessons where id=? and active=1",(lid,)).fetchone()
    ok=u and l and(u["id"]==l["teacher_id"] or q("select 1 from lesson_members where lesson_id=? and user_id=?",(lid,u["id"])).fetchone() or(l["class_id"] and q("select 1 from class_members where class_id=? and user_id=?",(l["class_id"],u["id"])).fetchone()))
    if not ok:
        await ws.close(code=4401)
        return
    await ws.accept()
    r=rooms.setdefault(lid,{"peers":{},"board":[],"idx":{},"writers":set(),"n":0,"page":None})
    r["n"]+=1
    pid=r["n"]
    host=u["id"]==l["teacher_id"]
    t0=time.time()
    if not host:
        q("insert into attendance(lesson_id,user_id,joins) values(?,?,1) on conflict(lesson_id,user_id) do update set joins=joins+1",(lid,u["id"]))
    others=[{"id":k,"name":v["name"],"host":v["host"],"hand":v["hand"],"uid":v["uid"]} for k,v in r["peers"].items()]
    r["peers"][pid]={"ws":ws,"name":u["name"],"host":host,"hand":False,"uid":u["id"],"t0":t0}
    await ws.send_json({"t":"init","id":pid,"host":host,"peers":others,"board":r["board"],"writers":list(r["writers"]),"page":r.get("page"),"roll":bool(r.get("roll")),"rec":bool(r.get("rec")),"poll":({"q":r["poll"]["q"],"opts":r["poll"]["opts"]} if r.get("poll") else None)})
    await cast(r,{"t":"join","id":pid,"name":u["name"],"host":host,"uid":u["id"]},pid)
    try:
        while True:
            m=await ws.receive_json()
            t=m.get("t")
            paint=host or u["id"] in r["writers"]
            if t=="sig":
                p=r["peers"].get(m.get("to"))
                if p:
                    await p["ws"].send_json({"t":"sig","from":pid,"d":m.get("d")})
            elif t=="chat":
                if not host:
                    q("update attendance set chats=chats+1 where lesson_id=? and user_id=?",(lid,u["id"]))
                await cast(r,{"t":"chat","name":u["name"],"text":str(m.get("text",""))[:500]})
            elif t=="hand":
                r["peers"][pid]["hand"]=bool(m.get("v"))
                if not host and r["peers"][pid]["hand"]:
                    q("update attendance set hands=hands+1 where lesson_id=? and user_id=?",(lid,u["id"]))
                await cast(r,{"t":"hand","id":pid,"v":r["peers"][pid]["hand"]})
            elif t=="lower" and host:
                p=r["peers"].get(m.get("id"))
                if p:
                    p["hand"]=False
                    await cast(r,{"t":"hand","id":m["id"],"v":False})
            elif t=="grant" and host:
                uid=m.get("uid")
                if m.get("v"):
                    r["writers"].add(uid)
                else:
                    r["writers"].discard(uid)
                await cast(r,{"t":"grant","uid":uid,"v":bool(m.get("v"))})
            elif t=="bookclose" and host:
                r["page"]=None
                await cast(r,{"t":"bookclose"})
            elif t=="page" and host:
                try:
                    bid=int(m.get("book_id"))
                    pg=int(m.get("page"))
                except (TypeError,ValueError):
                    continue
                b=q("select title,pages from books where id=?",(bid,)).fetchone()
                if b and 1<=pg<=b["pages"]:
                    q("insert or ignore into lesson_pages values(?,?,?,?)",(lid,bid,pg,time.time()))
                    r["page"]={"book_id":bid,"page":pg,"title":b["title"]}
                    await cast(r,{"t":"page",**r["page"]},pid)
            elif t=="roll" and host:
                r["roll"]=bool(m.get("v"))
                if r["roll"]:
                    r["here"]=set()
                    r["roll_at"]=time.time()
                await cast(r,{"t":"roll","v":r["roll"]})
            elif t=="here" and not host and r.get("roll"):
                r.setdefault("here",set()).add(u["id"])
                now=time.time()
                st="geç" if now-r.get("roll_at",now)>300 else "var"
                q("insert or replace into roll(lesson_id,user_id,at,status,early) values(?,?,?,?,0)",(lid,u["id"],now,st))
                await ws.send_json({"t":"here_ok"})
                await cast(r,{"t":"here_count","n":len(r["here"])})
            elif t=="poll" and host:
                opts=[str(x)[:80] for x in (m.get("opts") or [])][:6]
                if len(opts)>=2:
                    r["poll"]={"q":str(m.get("q",""))[:200],"opts":opts,"votes":{}}
                    await cast(r,{"t":"poll","q":r["poll"]["q"],"opts":opts})
            elif t=="vote" and not host and r.get("poll"):
                i=m.get("i")
                if isinstance(i,int) and 0<=i<len(r["poll"]["opts"]):
                    r["poll"]["votes"][u["id"]]=i
                    await cast(r,{"t":"pollres","counts":pcounts(r["poll"])})
            elif t=="pollend" and host and r.get("poll"):
                p=r["poll"]
                await cast(r,{"t":"pollres","counts":pcounts(p),"final":True,"q":p["q"],"opts":p["opts"]})
                r["poll"]=None
            elif t=="rec" and host:
                r["rec"]=bool(m.get("v"))
                await cast(r,{"t":"rec","v":r["rec"]})
            elif t=="draw" and paint:
                k=str(m.get("k"))
                s=r["idx"].get(k)
                if not s:
                    s={"k":k,"c":m.get("c"),"w":m.get("w"),"p":[]}
                    r["idx"][k]=s
                    r["board"].append(s)
                s["p"]+=m.get("p",[])
                await cast(r,m,pid)
            elif t=="undo" and paint:
                if r["board"]:
                    r["idx"].pop(r["board"].pop()["k"],None)
                await cast(r,m,pid)
            elif t=="clear" and paint:
                r["board"].clear()
                r["idx"].clear()
                await cast(r,m,pid)
    except Exception:
        pass
    finally:
        pe=r["peers"].pop(pid,None) or {"t0":t0}
        if not host:
            q("update roll set early=1 where lesson_id=? and user_id=? and exists(select 1 from lessons where id=? and active=1)",(lid,u["id"],lid))
            q("update attendance set secs=secs+? where lesson_id=? and user_id=?",(int(time.time()-pe["t0"]),lid,u["id"]))
        await cast(r,{"t":"leave","id":pid})

# =====================================================================
#  SINIFLAR
# =====================================================================
KINDS=("sozlu","yazili","deneme","diger")
class Cls(BaseModel):
    name:str
class Stu(BaseModel):
    text:str=""
class Gr(BaseModel):
    class_id:int
    student_id:int
    kind:str
    score:float
    note:str=""
class Top(BaseModel):
    topic:str=""
class Guest(BaseModel):
    name:str
    code:str
def own_class(cid,u):
    c=q("select * from classes where id=? and teacher_id=?",(cid,u["id"])).fetchone()
    if not c:
        raise HTTPException(404,"Sınıf bulunamadı")
    return c
def link_roster(u):
    if u["role"]!="student":
        return
    for r in q("select class_id from class_roster where email=?",(u["email"],)).fetchall():
        q("insert or ignore into class_members(class_id,user_id) values(?,?)",(r["class_id"],u["id"]))
PENDING="select count(*) from class_roster r where r.class_id=c.id and not exists(select 1 from users x join class_members m on m.user_id=x.id where m.class_id=r.class_id and x.email=r.email)"
@app.post("/api/classes")
def add_class(c:Cls,u=Depends(teacher)):
    n=c.name.strip()[:30]
    if not n:
        raise HTTPException(400,"Sınıf adı gerekli")
    code=new_code()
    return {"id":q("insert into classes(teacher_id,name,code) values(?,?,?)",(u["id"],n,code)).lastrowid,"code":code}
@app.get("/api/classes")
def classes(u=Depends(me)):
    if u["role"]=="teacher":
        rows=q(f"select c.id,c.name,c.code,(select count(*) from class_members m where m.class_id=c.id) members,({PENDING}) pending from classes c where c.teacher_id=? order by c.name",(u["id"],)).fetchall()
    else:
        rows=q("select c.id,c.name,t.name teacher from classes c join class_members m on m.class_id=c.id and m.user_id=? join users t on t.id=c.teacher_id order by c.name",(u["id"],)).fetchall()
    return [dict(r) for r in rows]
@app.post("/api/classes/join")
def join_class(c:Code,u=Depends(me)):
    if u["role"]!="student":
        raise HTTPException(403,"Sadece öğrenci")
    r=q("select * from classes where code=?",(c.code.strip().upper(),)).fetchone()
    if not r:
        raise HTTPException(404,"Geçersiz sınıf kodu")
    q("insert or ignore into class_members(class_id,user_id) values(?,?)",(r["id"],u["id"]))
    return {"id":r["id"],"name":r["name"]}
@app.get("/api/compare")
def compare(u=Depends(teacher)):
    now=time.time()
    out=[]
    for c in q("select id,name from classes where teacher_id=? order by name",(u["id"],)).fetchall():
        cid=c["id"]
        members=q("select count(*) n from class_members where class_id=?",(cid,)).fetchone()["n"]
        sc=q("select avg(a.score) s from attempts a join exams e on e.id=a.exam_id where e.class_id=? and e.scope='class' and e.private=0 and a.status='bitti' and e.end_at<?",(cid,now)).fetchone()["s"]
        nles=q("select count(*) n from lessons where class_id=?",(cid,)).fetchone()["n"]
        att=q("select count(*) n from attendance where lesson_id in(select id from lessons where class_id=?)",(cid,)).fetchone()["n"]
        part=round(att/(nles*members),2) if nles and members else None
        tt={}
        for e in q("select id from exams where class_id=? and scope='class' and private=0 and end_at<?",(cid,now)).fetchall():
            qs={x["id"]:(x["ans"],x["topic"]) for x in q("select id,ans,topic from questions where exam_id=?",(e["id"],)).fetchall()}
            for a in q("select answers from attempts where exam_id=? and status='bitti'",(e["id"],)).fetchall():
                ans=json.loads(a["answers"] or "{}")
                for qid,(ca,tp) in qs.items():
                    if not tp:
                        continue
                    t=tt.setdefault(tp,[0,0])
                    t[0]+=1 if ans.get(str(qid))==ca else 0
                    t[1]+=1
        cands=[(k,round(100*v[0]/v[1])) for k,v in tt.items() if v[1]>=3]
        weak=min(cands,key=lambda x:x[1]) if cands else None
        out.append({"id":cid,"name":c["name"],"members":members,"avg":round(sc,1) if sc is not None else None,"part":part,"weak":{"topic":weak[0],"rate":weak[1]} if weak else None})
    return out
@app.get("/api/classes/{cid}")
def class_detail(cid:int,u=Depends(teacher)):
    c=own_class(cid,u)
    mem=q("select u.id,u.name,u.email,m.active from class_members m join users u on u.id=m.user_id where m.class_id=? order by u.name",(cid,)).fetchall()
    ga={r["student_id"]:(round(r["a"],1),r["n"]) for r in q("select student_id,avg(score) a,count(*) n from grades where class_id=? group by student_id",(cid,)).fetchall()}
    mem=[dict(r)|{"gavg":ga.get(r["id"],(None,0))[0],"gn":ga.get(r["id"],(None,0))[1]} for r in mem]
    pend=q("select id,name,email from class_roster r where class_id=? and not exists(select 1 from users x join class_members m on m.user_id=x.id where m.class_id=r.class_id and x.email=r.email) order by name",(cid,)).fetchall()
    return {"id":c["id"],"name":c["name"],"code":c["code"],"members":mem,"pending":[dict(r) for r in pend]}
class Act(BaseModel):
    active:int
@app.post("/api/classes/{cid}/members/{uid}/active")
def set_active(cid:int,uid:int,a:Act,u=Depends(teacher)):
    own_class(cid,u)
    q("update class_members set active=? where class_id=? and user_id=?",(1 if a.active else 0,cid,uid))
    return {"ok":True}
@app.get("/api/classes/{cid}/students/{uid}/profile")
def student_profile(cid:int,uid:int,u=Depends(teacher)):
    own_class(cid,u)
    return profile_for(cid,uid)
@app.get("/api/me/progress")
def my_progress(u=Depends(me)):
    if u["role"]!="student":
        raise HTTPException(403,"Yalnızca öğrenciler")
    c=q("select class_id from class_members where user_id=? limit 1",(u["id"],)).fetchone()
    if not c:
        raise HTTPException(404,"Henüz bir sınıfa bağlı değilsin")
    return profile_for(c["class_id"],u["id"])
def profile_for(cid,uid):
    sweep()
    if not q("select 1 from class_members where class_id=? and user_id=?",(cid,uid)).fetchone():
        raise HTTPException(404,"Öğrenci bu sınıfta değil")
    exs=q("select * from exams where class_id=? and scope='class' and end_at<? order by start_at",(cid,time.time())).fetchall()
    rows=[];tt={}
    for e in exs:
        if e["private"] and not q("select 1 from exam_members where exam_id=? and user_id=?",(e["id"],uid)).fetchone():
            continue
        a=q("select * from attempts where exam_id=? and user_id=? and status='bitti' order by id desc limit 1",(e["id"],uid)).fetchone()
        if not a:
            continue
        ans=json.loads(a["answers"] or "{}")
        for x in q("select id,ans,topic from questions where exam_id=?",(e["id"],)).fetchall():
            if not x["topic"]:
                continue
            t=tt.setdefault(x["topic"],[0,0])
            t[0]+=1 if ans.get(str(x["id"]))==x["ans"] else 0
            t[1]+=1
        rows.append({"title":e["title"],"score":a["score"]})
    topics=sorted([{"topic":k,"ok":v[0],"n":v[1],"rate":round(100*v[0]/v[1])} for k,v in tt.items()],key=lambda x:x["rate"])
    tr="na"
    if len(rows)>=2:
        base=sum(x["score"] for x in rows[:-1])/len(rows[:-1])
        d=rows[-1]["score"]-base
        tr="up" if d>=5 else "down" if d<=-5 else "flat"
    lessons=[r["id"] for r in q("select id from lessons where class_id=? order by id",(cid,)).fetchall()]
    ph=",".join("?"*len(lessons)) or "null"
    at=q(f"select count(*) n from attendance where user_id=? and lesson_id in({ph})",(uid,*lessons)).fetchone()
    gs=[dict(r) for r in q("select kind,score,note from grades where class_id=? and student_id=? order by at",(cid,uid)).fetchall()]
    return {"exams":rows,"topics":topics,"strong":[t for t in topics if t["n"]>=2 and t["rate"]>=75],"weak":[t for t in topics if t["n"]>=2 and t["rate"]<50],"trend":tr,"attended":at["n"],"lessons":len(lessons),"part_rate":round(at["n"]/len(lessons),2) if lessons else None,"grades":gs}
@app.get("/api/classes/{cid}/diary")
def class_diary(cid:int,u=Depends(teacher)):
    own_class(cid,u)
    rows=q("select l.id,l.title,l.topic,l.ended_at,l.active,(select count(*) from attendance a where a.lesson_id=l.id) n,(select min(at) from lesson_pages p where p.lesson_id=l.id) first_at from lessons l where l.class_id=? order by l.id desc",(cid,)).fetchall()
    out=[]
    for r in rows:
        pg=[dict(x) for x in q("select b.title,p.page from lesson_pages p join books b on b.id=p.book_id where p.lesson_id=? order by p.at",(r["id"],)).fetchall()]
        out.append({"id":r["id"],"title":r["title"],"topic":r["topic"],"ended_at":r["ended_at"],"first_at":r["first_at"],"attendees":r["n"],"pages":pg,"active":r["active"]})
    return out
@app.delete("/api/classes/{cid}")
def del_class(cid:int,u=Depends(teacher)):
    own_class(cid,u)
    for t in("class_members","class_roster","grades"):
        q(f"delete from {t} where class_id=?",(cid,))
    q("update lessons set class_id=null where class_id=?",(cid,))
    q("update exams set class_id=null where class_id=?",(cid,))
    q("delete from classes where id=?",(cid,))
    return {"ok":True}
@app.post("/api/classes/{cid}/students")
def add_students(cid:int,s:Stu,u=Depends(teacher)):
    own_class(cid,u)
    added=linked=skipped=0
    for line in s.text.splitlines():
        line=line.strip()
        if not line:
            continue
        m=re.search(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+",line)
        if not m:
            skipped+=1
            continue
        email=m.group(0).lower()
        name=re.sub(r"[,;\t]+"," ",line.replace(m.group(0),"")).strip()[:60] or email.split("@")[0]
        q("insert or ignore into class_roster(class_id,name,email) values(?,?,?)",(cid,name,email))
        usr=q("select id from users where email=? and role='student'",(email,)).fetchone()
        if usr:
            q("insert or ignore into class_members(class_id,user_id) values(?,?)",(cid,usr["id"]))
            linked+=1
        added+=1
    return {"added":added,"linked":linked,"skipped":skipped}
@app.delete("/api/classes/{cid}/students/{uid}")
def rm_member(cid:int,uid:int,u=Depends(teacher)):
    own_class(cid,u)
    q("delete from class_roster where class_id=? and email=(select email from users where id=?)",(cid,uid))
    q("delete from class_members where class_id=? and user_id=?",(cid,uid))
    audit(u,"öğrenci sınıftan çıkarma",f"sınıf {cid} öğrenci {uid}")
    return {"ok":True}
@app.delete("/api/classes/{cid}/roster/{rid}")
def rm_roster(cid:int,rid:int,u=Depends(teacher)):
    own_class(cid,u)
    q("delete from class_roster where id=? and class_id=?",(rid,cid))
    return {"ok":True}

# ---------- misafir ----------
@app.post("/api/guest")
def guest(g:Guest):
    name=g.name.strip()[:40]
    code=g.code.strip().upper()
    if not name:
        raise HTTPException(400,"Adını yaz")
    l=q("select * from lessons where code=? and active=1 and guests=1",(code,)).fetchone()
    e=None if l else q("select * from exams where code=? and guests=1 and scope='class' and end_at>?",(code,time.time())).fetchone()
    if not l and not e:
        raise HTTPException(404,"Kod geçersiz ya da öğretmen misafir kabul etmiyor")
    s=secrets.token_hex(16)
    uid=q("insert into users(name,email,pw,salt,role) values(?,?,?,?,?)",(name+" (misafir)","guest-"+secrets.token_hex(6)+"@guest.local",hp(secrets.token_hex(8),s),s,"guest")).lastrowid
    if l:
        q("insert or ignore into lesson_members values(?,?)",(l["id"],uid))
    else:
        q("insert or ignore into exam_members values(?,?)",(e["id"],uid))
    u=q("select * from users where id=?",(uid,)).fetchone()
    return {"token":start(uid),"user":pub(u),"lesson":{"id":l["id"],"title":l["title"]} if l else None,"exam":{"id":e["id"],"title":e["title"]} if e else None}

# =====================================================================
#  DERSLER: sınıf bağlantısı, işlenen konular
# =====================================================================
@app.get("/api/lessons/active")
def active_lessons(u=Depends(me)):
    rows=q("select l.id,l.title,t.name teacher,c.name cname from lessons l join users t on t.id=l.teacher_id left join classes c on c.id=l.class_id where l.active=1 and (l.class_id in(select class_id from class_members where user_id=?) or l.id in(select lesson_id from lesson_members where user_id=?)) order by l.id desc",(u["id"],u["id"])).fetchall()
    return [dict(r) for r in rows]
@app.post("/api/lessons/{lid}/topic")
def lesson_topic(lid:int,t:Top,u=Depends(teacher)):
    if not q("update lessons set topic=? where id=? and teacher_id=?",(t.topic.strip()[:300],lid,u["id"])).rowcount:
        raise HTTPException(404,"Ders bulunamadı")
    return {"ok":True}
def month_range(m):
    if not re.fullmatch(r"\d{4}-\d{2}",m or ""):
        return None
    y,mo=int(m[:4]),int(m[5:])
    if not 1<=mo<=12:
        return None
    return datetime.datetime(y,mo,1).timestamp(),datetime.datetime(y+(mo==12),mo%12+1,1).timestamp()
def compact(pages):
    out=[]
    s=p=None
    for x in sorted(set(pages)):
        if s is None:
            s=p=x
        elif x==p+1:
            p=x
        else:
            out.append(f"{s}-{p}" if p>s else str(s))
            s=p=x
    if s is not None:
        out.append(f"{s}-{p}" if p>s else str(s))
    return ", ".join(out)
def parse_pages(s):
    out=set()
    for part in re.split(r"[,\s;]+",(s or "").strip()):
        m=re.fullmatch(r"(\d+)(?:-(\d+))?",part)
        if m:
            a=int(m[1])
            b=int(m[2] or a)
            if b<a:
                a,b=b,a
            out.update(range(a,min(b,a+300)+1))
    return sorted(out)[:300]
def covered(cid,month=""):
    sql="select id,title,topic,started_at from lessons where class_id=?"
    a=[cid]
    mr=month_range(month)
    if mr:
        sql+=" and coalesce(started_at,0)>=? and coalesce(started_at,0)<?"
        a+=list(mr)
    out=[]
    for l in q(sql+" order by coalesce(started_at,0)",a).fetchall():
        pg=q("select p.book_id,p.page,b.title from lesson_pages p join books b on b.id=p.book_id where p.lesson_id=? order by p.book_id,p.page",(l["id"],)).fetchall()
        books={}
        for r in pg:
            books.setdefault(r["book_id"],{"book_id":r["book_id"],"title":r["title"],"list":[]})["list"].append(r["page"])
        out.append({"id":l["id"],"title":l["title"],"topic":l["topic"] or "","at":l["started_at"],"books":[{"book_id":b["book_id"],"title":b["title"],"pages":compact(b["list"]),"n":len(b["list"])} for b in books.values()],"pairs":[(r["book_id"],r["page"]) for r in pg]})
    return out
@app.get("/api/classes/{cid}/covered")
def covered_ep(cid:int,month:str="",u=Depends(teacher)):
    own_class(cid,u)
    return [{k:v for k,v in l.items() if k!="pairs"} for l in covered(cid,month)]

# =====================================================================
#  NOTLAR (sözlü / yazılı / deneme)
# =====================================================================
@app.post("/api/grades")
def add_grade(g:Gr,u=Depends(teacher)):
    own_class(g.class_id,u)
    audit(u,"not ekleme",f"sınıf {g.class_id} öğrenci {g.student_id} {g.kind} {g.score}")
    if not q("select 1 from class_members where class_id=? and user_id=?",(g.class_id,g.student_id)).fetchone():
        raise HTTPException(404,"Öğrenci bu sınıfta değil")
    if not 0<=g.score<=100:
        raise HTTPException(400,"Not 0-100 arasında olmalı")
    k=g.kind if g.kind in KINDS else "diger"
    return {"id":q("insert into grades(class_id,student_id,teacher_id,kind,score,note,at) values(?,?,?,?,?,?,?)",(g.class_id,g.student_id,u["id"],k,g.score,g.note.strip()[:100],time.time())).lastrowid}
@app.delete("/api/grades/{gid}")
def del_grade(gid:int,u=Depends(teacher)):
    if not q("delete from grades where id=? and teacher_id=?",(gid,u["id"])).rowcount:
        raise HTTPException(404,"Not bulunamadı")
    return {"ok":True}
@app.get("/api/classes/{cid}/grades")
def class_grades(cid:int,u=Depends(teacher)):
    own_class(cid,u)
    return [dict(r) for r in q("select * from grades where class_id=? order by at",(cid,)).fetchall()]

# =====================================================================
#  YAPAY ZEKA (Gemini / OpenRouter)
# =====================================================================
GM="gemini-2.5-flash"
OM="meta-llama/llama-3.3-70b-instruct:free"
class Sett(BaseModel):
    provider:str="gemini"
    gemini_key:str|None=None
    gemini_model:str=""
    or_key:str|None=None
    or_model:str=""
    share:int=0
    clear_gemini:bool=False
    clear_or:bool=False
class Gen(BaseModel):
    source:str="topics"
    topics:str=""
    book_id:int|None=None
    pages:str=""
    class_id:int|None=None
    month:str=""
    count:int=10
    level:str="orta"
    opts:int=4
def mdl(s,default):
    s=(s or "").strip()
    return s if re.fullmatch(r"[\w.\-:/]{1,100}",s) else default
def hint(k):
    return "…"+k[-4:] if k else ""
def http(url,body=None,headers=None,timeout=90):
    h={"Content-Type":"application/json",**(headers or {})}
    req=urllib.request.Request(url,data=json.dumps(body).encode() if body is not None else None,headers=h,method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8","ignore"))
    except urllib.error.HTTPError as e:
        raw=e.read().decode("utf-8","ignore")[:300]
        msg={400:"İstek reddedildi (anahtar ya da model adı hatalı olabilir)",401:"API anahtarı geçersiz",403:"API anahtarı bu işlem için yetkili değil",404:"Model bulunamadı, model adını kontrol et",429:"Ücretsiz kullanım limiti doldu, biraz bekleyip tekrar dene"}.get(e.code,f"Yapay zeka hatası ({e.code})")
        raise HTTPException(502,msg+(": "+raw if e.code not in(400,401,403,404,429) and raw else ""))
    except Exception as e:
        raise HTTPException(502,"Yapay zekaya ulaşılamadı: "+str(e)[:120])
def pick_ai(r):
    if r["provider"]=="openrouter" and r["or_key"]:
        return "openrouter",r["or_key"],r["or_model"] or OM
    if r["gemini_key"]:
        return "gemini",r["gemini_key"],r["gemini_model"] or GM
    if r["or_key"]:
        return "openrouter",r["or_key"],r["or_model"] or OM
    return None
def ai_for(u):
    r=q("select * from settings where user_id=?",(u["id"],)).fetchone()
    p=pick_ai(r) if r else None
    if p:
        return (*p,False)
    if u["role"]=="student":
        r=q("select s.* from settings s join classes c on c.teacher_id=s.user_id join class_members m on m.class_id=c.id where m.user_id=? and s.share=1 and (s.gemini_key!='' or s.or_key!='') limit 1",(u["id"],)).fetchone()
        p=pick_ai(r) if r else None
        if p:
            return (*p,True)
    raise HTTPException(400,"Yapay zeka için Ayarlar bölümünden bir API anahtarı gir")
def ai_text(u,prompt,js=False):
    if u["role"] not in("teacher","student"):
        raise HTTPException(403,"Yapay zeka misafirlere kapalı")
    prov,key,model,shared=ai_for(u)
    if shared:
        n=q("select count(*) from ai_log where user_id=? and at>?",(u["id"],time.time()-86400)).fetchone()[0]
        if n>=10:
            raise HTTPException(429,"Bugünlük ortak yapay zeka hakkın doldu (günde 10). Kendi anahtarını Ayarlar'dan girebilirsin.")
        q("insert into ai_log values(?,?)",(u["id"],time.time()))
    if prov=="gemini":
        cfg={"temperature":0.4}
        if js:
            cfg["responseMimeType"]="application/json"
        d=http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",{"contents":[{"parts":[{"text":prompt}]}],"generationConfig":cfg},{"x-goog-api-key":key})
        try:
            return d["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError,IndexError,TypeError):
            raise HTTPException(502,"Yapay zeka boş yanıt verdi (içerik filtresine takılmış olabilir), tekrar dene")
    d=http("https://openrouter.ai/api/v1/chat/completions",{"model":model,"messages":[{"role":"user","content":prompt}],"temperature":0.4},{"Authorization":"Bearer "+key})
    try:
        t=d["choices"][0]["message"]["content"]
    except (KeyError,IndexError,TypeError):
        raise HTTPException(502,"Yapay zeka yanıt vermedi (model meşgul olabilir), tekrar dene")
    if not t:
        raise HTTPException(502,"Yapay zeka boş yanıt verdi, tekrar dene")
    return t
@app.get("/api/settings")
def get_settings(u=Depends(me)):
    r=q("select * from settings where user_id=?",(u["id"],)).fetchone()
    shared=False
    if u["role"]=="student":
        shared=bool(q("select 1 from settings s join classes c on c.teacher_id=s.user_id join class_members m on m.class_id=c.id where m.user_id=? and s.share=1 and (s.gemini_key!='' or s.or_key!='')",(u["id"],)).fetchone())
    if not r:
        return {"provider":"gemini","gemini_set":False,"gemini_hint":"","gemini_model":GM,"or_set":False,"or_hint":"","or_model":OM,"share":0,"shared_available":shared}
    return {"provider":r["provider"],"gemini_set":bool(r["gemini_key"]),"gemini_hint":hint(r["gemini_key"]),"gemini_model":r["gemini_model"] or GM,"or_set":bool(r["or_key"]),"or_hint":hint(r["or_key"]),"or_model":r["or_model"] or OM,"share":r["share"],"shared_available":shared}
@app.put("/api/settings")
def put_settings(s:Sett,u=Depends(me)):
    if u["role"] not in("teacher","student"):
        raise HTTPException(403,"Yetki yok")
    q("insert or ignore into settings(user_id) values(?)",(u["id"],))
    r=q("select * from settings where user_id=?",(u["id"],)).fetchone()
    gk="" if s.clear_gemini else (s.gemini_key.strip() if s.gemini_key and s.gemini_key.strip() else r["gemini_key"])
    ok="" if s.clear_or else (s.or_key.strip() if s.or_key and s.or_key.strip() else r["or_key"])
    q("update settings set provider=?,gemini_key=?,gemini_model=?,or_key=?,or_model=?,share=? where user_id=?",("openrouter" if s.provider=="openrouter" else "gemini",gk,mdl(s.gemini_model,GM),ok,mdl(s.or_model,OM),1 if (s.share and u["role"]=="teacher") else 0,u["id"]))
    return {"ok":True}
@app.post("/api/ai/test")
def ai_test(u=Depends(me)):
    return {"reply":ai_text(u,"Yalnızca tek kelime yaz: Merhaba").strip()[:80]}
@app.get("/api/ai/models")
def ai_models(provider:str="openrouter",u=Depends(me)):
    if provider=="gemini":
        r=q("select gemini_key from settings where user_id=?",(u["id"],)).fetchone()
        if not r or not r["gemini_key"]:
            raise HTTPException(400,"Önce Gemini anahtarını kaydet")
        d=http("https://generativelanguage.googleapis.com/v1beta/models?pageSize=100",None,{"x-goog-api-key":r["gemini_key"]})
        ids=[m["name"].split("/",1)[-1] for m in d.get("models",[]) if "generateContent" in m.get("supportedGenerationMethods",[]) and m.get("name","").startswith("models/gemini")]
        return sorted(ids)[:80]
    d=http("https://openrouter.ai/api/v1/models")
    return sorted(m["id"] for m in d.get("data",[]) if str(m.get("id","")).endswith(":free"))[:80]
def pages_ctx(pairs,limit=14000):
    pairs=list(dict.fromkeys(pairs))
    if len(pairs)>24:
        step=len(pairs)/24
        pairs=[pairs[int(i*step)] for i in range(24)]
    if not pairs:
        return ""
    per=max(300,limit//len(pairs))
    parts=[]
    titles={}
    for b,p in pairs:
        r=q("select text from texts where book_id=? and page=?",(b,p)).fetchone()
        t=" ".join((r["text"] if r else "").split())
        if not t:
            continue
        if b not in titles:
            tr=q("select title from books where id=?",(b,)).fetchone()
            titles[b]=tr["title"] if tr else str(b)
        parts.append(f"[{titles[b]} — sayfa {p}]\n{t[:per]}")
    return "\n\n".join(parts)
def parse_qs(txt):
    t=re.sub(r"^```(?:json)?|```$","",(txt or "").strip(),flags=re.M).strip()
    try:
        d=json.loads(t)
    except Exception:
        m=re.search(r"[\[{].*[\]}]",t,re.S)
        try:
            d=json.loads(m.group(0)) if m else []
        except Exception:
            return []
    arr=d.get("questions",[]) if isinstance(d,dict) else d
    out=[]
    for x in arr if isinstance(arr,list) else []:
        if not isinstance(x,dict):
            continue
        text=str(x.get("q") or x.get("question") or x.get("text") or "").strip()
        o=[re.sub(r"^[A-Ea-e][\)\.\:]\s+","",str(i).strip()) for i in (x.get("options") or x.get("choices") or [])][:5]
        a=x.get("answer",x.get("ans"))
        if isinstance(a,str):
            s=a.strip()
            if re.fullmatch(r"[A-Ea-e][\)\.]?",s):
                a="ABCDE".index(s[0].upper())
            elif s.isdigit():
                a=int(s)
            else:
                a=o.index(s) if s in o else -1
        if text and len(o)>=2 and all(o) and isinstance(a,int) and not isinstance(a,bool) and 0<=a<len(o):
            out.append({"text":text,"opts":o,"ans":a,"expl":str(x.get("explanation") or x.get("expl") or "").strip()[:400],"topic":str(x.get("topic") or "").strip()[:80]})
    return out
def ai_job_run(jid,u,g):
    try:
        out=ai_questions(g,u)
        JOBS[jid]={"status":"done","questions":out["questions"],"uid":u["id"]}
    except HTTPException as e:
        JOBS[jid]={"status":"error","error":str(e.detail),"uid":u["id"]}
    except Exception as e:
        JOBS[jid]={"status":"error","error":"Üretim başarısız oldu: "+str(e)[:200],"uid":u["id"]}
@app.post("/api/ai/jobs")
def ai_job(g:Gen,u=Depends(me)):
    jid=secrets.token_hex(8)
    JOBS[jid]={"status":"running","uid":u["id"]}
    threading.Thread(target=ai_job_run,args=(jid,dict(u),g),daemon=True).start()
    return {"job":jid}
@app.get("/api/ai/jobs/{jid}")
def ai_job_get(jid:str,u=Depends(me)):
    j=JOBS.get(jid)
    if not j or j["uid"]!=u["id"]:
        raise HTTPException(404,"İş bulunamadı")
    out={k:v for k,v in j.items() if k!="uid"}
    if j["status"]!="running":
        JOBS.pop(jid,None)
    return out
@app.post("/api/ai/questions")
def ai_questions(g:Gen,u=Depends(me)):
    n=max(1,min(g.count,30))
    k=5 if g.opts==5 else 4
    level=g.level if g.level in("kolay","orta","zor") else "orta"
    ctx=""
    if g.source=="pages":
        if not g.book_id:
            raise HTTPException(400,"Kitap seç")
        can(g.book_id,u)
        pgs=parse_pages(g.pages)
        if not pgs:
            raise HTTPException(400,"Sayfa aralığı gir (örn. 12-18, 22)")
        ctx=pages_ctx([(g.book_id,p) for p in pgs])
        if not ctx.strip():
            raise HTTPException(400,"Seçilen sayfalarda metin bulunamadı (taranmış PDF olabilir)")
    elif g.source=="month":
        if u["role"]!="teacher" or not g.class_id:
            raise HTTPException(403,"Bu seçenek sınıf seçen öğretmenler içindir")
        own_class(g.class_id,u)
        ls=covered(g.class_id,g.month)
        parts=[]
        pairs=[]
        for l in ls:
            pairs+=l["pairs"]
            head=l["title"]+(": "+l["topic"] if l["topic"] else "")
            parts.append("Ders: "+head)
        ctx=("İşlenen dersler:\n"+"\n".join(parts)+"\n\n" if parts else "")+pages_ctx(pairs)
        if not parts:
            raise HTTPException(400,"Bu ay bu sınıf için kayıtlı ders yok")
    if not ctx.strip() and not g.topics.strip():
        raise HTTPException(400,"Konu yaz ya da bir kaynak seç")
    extra=("\nÖğretmenin/öğrencinin isteği ya da konuları: "+g.topics.strip()[:600]) if g.topics.strip() else ""
    prompt=f"""Sen deneyimli bir Türk öğretmensin. Aşağıdaki kaynağa dayanarak {n} adet çoktan seçmeli soru hazırla.
Seviye: {level}. Her soruda {k} seçenek olsun, yalnızca biri doğru olsun. Sorular ve seçenekler Türkçe olsun.
Kaynakta olmayan bilgiyi sorma. Yanlış seçenekler inandırıcı ama kesin biçimde yanlış olsun. Doğru seçeneğin yerini dengeli dağıt.
Çıktı YALNIZCA geçerli JSON olsun, başka açıklama ya da kod bloğu yazma. Biçim:
{{"questions":[{{"q":"soru metni","options":["seçenek 1","seçenek 2","seçenek 3","seçenek 4"],"answer":0,"explanation":"doğru cevabın kısa gerekçesi","topic":"kısa konu başlığı"}}]}}
"answer" doğru seçeneğin 0'dan başlayan sırasıdır. Seçenek metinlerine "A)" gibi harf ekleme.{extra}

KAYNAK:
{ctx or "(Kaynak yok; konuyu müfredat bilginle işle.)"}"""
    out=parse_qs(ai_text(u,prompt,True))
    if not out:
        raise HTTPException(502,"Yapay zeka geçerli soru üretemedi, tekrar dene (daha az soru istemek yardımcı olabilir)")
    if g.topics.strip():
        for x in out:
            x["topic"]=x["topic"] or g.topics.strip()[:60]
    return {"questions":out[:n]}

# =====================================================================
#  SINAVLAR
# =====================================================================
class Qn(BaseModel):
    text:str
    opts:list[str]
    ans:int
    expl:str=""
    topic:str=""
class PoolIn(BaseModel):
    questions:list[Qn]
@app.get("/api/pool")
def pool_list(u=Depends(teacher)):
    rows=q("select id,topic,q,opts,ans,expl from question_pool where teacher_id=? order by id desc limit 300",(u["id"],)).fetchall()
    return [{"id":r["id"],"topic":r["topic"],"text":r["q"],"opts":json.loads(r["opts"]),"ans":r["ans"],"expl":r["expl"]} for r in rows]
@app.post("/api/pool")
def pool_add(p:PoolIn,u=Depends(teacher)):
    n=0
    for x in p.questions:
        o=[s.strip() for s in x.opts]
        if not x.text.strip() or not 2<=len(o)<=5 or not all(o) or not 0<=x.ans<len(o):
            continue
        q("insert into question_pool(teacher_id,topic,q,opts,ans,expl,created) values(?,?,?,?,?,?,?)",(u["id"],x.topic.strip()[:80],x.text.strip(),json.dumps(o,ensure_ascii=False),x.ans,x.expl.strip()[:400],time.time()))
        n+=1
    if not n:
        raise HTTPException(400,"Geçerli soru yok (metin, en az 2 dolu şık ve doğru cevap gerekli)")
    return {"added":n}
@app.delete("/api/pool/{pid}")
def pool_del(pid:int,u=Depends(teacher)):
    q("delete from question_pool where id=? and teacher_id=?",(pid,u["id"]))
    return {"ok":True}
class Exm(BaseModel):
    title:str
    kind:str="sinav"
    class_id:int|None=None
    start_at:float|None=None
    end_at:float|None=None
    duration:int=0
    guests:int=0
    shuffle:int=1
    source:str=""
    private:int=0
    students:list[int]=[]
    questions:list[Qn]
class Ans(BaseModel):
    answers:dict[str,int]={}
class Fin(BaseModel):
    answers:dict[str,int]|None=None
class Ev(BaseModel):
    k:str
def estate(e,now=None):
    now=now or time.time()
    if e["scope"]=="self":
        return "open"
    return "upcoming" if now<e["start_at"] else "open" if now<e["end_at"] else "closed"
def exam_for(eid,u):
    e=q("select * from exams where id=?",(eid,)).fetchone()
    if e:
        if e["owner_id"]==u["id"]:
            return e
        if e["scope"]=="class" and(((not e["private"]) and e["class_id"] and q("select 1 from class_members where class_id=? and user_id=?",(e["class_id"],u["id"])).fetchone()) or q("select 1 from exam_members where exam_id=? and user_id=?",(eid,u["id"])).fetchone()):
            return e
    raise HTTPException(404,"Sınav bulunamadı")
def my_attempt(aid,u):
    a=q("select * from attempts where id=? and user_id=?",(aid,u["id"])).fetchone()
    if not a:
        raise HTTPException(404,"Deneme bulunamadı")
    return a
def grade(a):
    qs=q("select id,ans from questions where exam_id=?",(a["exam_id"],)).fetchall()
    ans=json.loads(a["answers"] or "{}")
    c=w=b=0
    for x in qs:
        v=ans.get(str(x["id"]))
        if v is None or v<0:
            b+=1
        elif v==x["ans"]:
            c+=1
        else:
            w+=1
    return c,w,b,round(100*c/(len(qs) or 1),1)
def go_back(a,now):
    if a["away_since"] is None:
        return
    d=int(max(0,now-a["away_since"]))
    evs=json.loads(a["events"] or "[]")
    evs.append({"k":"back","at":now,"d":d})
    q("update attempts set away_since=null,away_sec=away_sec+?,events=? where id=?",(d,json.dumps(evs[-300:]),a["id"]))
def close_attempt(aid):
    a=q("select * from attempts where id=?",(aid,)).fetchone()
    if not a or a["status"]!="devam":
        return
    fin=min(time.time(),a["deadline"])
    go_back(a,fin)
    c,w,b,s=grade(a)
    q("update attempts set status='bitti',finished=?,correct=?,wrong=?,blank=?,score=? where id=?",(fin,c,w,b,s,aid))
def sweep():
    for r in q("select id from attempts where status='devam' and deadline<?",(time.time(),)).fetchall():
        close_attempt(r["id"])
def prev_for(e,uid):
    r=q("select e2.title,a.score from exams e2 join attempts a on a.exam_id=e2.id where a.user_id=? and a.status='bitti' and e2.id!=? and e2.owner_id=? and e2.scope=? and coalesce(e2.class_id,0)=coalesce(?,0) and coalesce(e2.start_at,e2.created)<coalesce(?,?) order by coalesce(e2.start_at,e2.created) desc,a.id desc limit 1",(uid,e["id"],e["owner_id"],e["scope"],e["class_id"],e["start_at"],e["created"])).fetchone()
    return dict(r) if r else None
@app.post("/api/exams")
def add_exam(e:Exm,u=Depends(me)):
    if u["role"] not in("teacher","student"):
        raise HTTPException(403,"Yetki yok")
    if not e.title.strip():
        raise HTTPException(400,"Başlık gerekli")
    if not 1<=len(e.questions)<=100:
        raise HTTPException(400,"1-100 arası soru olmalı")
    qs=[]
    for i,x in enumerate(e.questions,1):
        o=[s.strip() for s in x.opts]
        if not x.text.strip() or not 2<=len(o)<=5 or not all(o):
            raise HTTPException(400,f"{i}. soru eksik: soru metni ve en az 2 dolu seçenek gerekli")
        if not 0<=x.ans<len(o):
            raise HTTPException(400,f"{i}. soruda doğru cevap seçilmemiş")
        qs.append((x.text.strip(),json.dumps(o,ensure_ascii=False),x.ans,x.expl.strip()[:400],x.topic.strip()[:80]))
    kind=e.kind if e.kind in("sinav","deneme") else "sinav"
    dur=max(0,min(e.duration,600))
    cid=st=en=code=None
    gu=0
    scope="self"
    if u["role"]=="teacher":
        scope="class"
        if e.class_id:
            own_class(e.class_id,u)
        if not e.start_at or not e.end_at or e.end_at<=e.start_at:
            raise HTTPException(400,"Başlangıç ve bitiş saatini doğru gir")
        if e.end_at<time.time():
            raise HTTPException(400,"Bitiş saati geçmişte olamaz")
        gu=0 if e.private else (1 if e.guests else 0)
        if not e.class_id and not gu:
            raise HTTPException(400,"Bir sınıf seç ya da misafir kabulünü aç")
        cid,st,en=e.class_id,e.start_at,e.end_at
        code=new_code()
    eid=q("insert into exams(owner_id,class_id,title,kind,scope,start_at,end_at,duration,guests,shuffle,code,created,source) values(?,?,?,?,?,?,?,?,?,?,?,?,?)",(u["id"],cid,e.title.strip()[:120],kind,scope,st,en,dur,gu,1 if e.shuffle else 0,code,time.time(),e.source[:20])).lastrowid
    for i,x in enumerate(qs):
        q("insert into questions(exam_id,idx,q,opts,ans,expl,topic) values(?,?,?,?,?,?,?)",(eid,i,*x))
    if u["role"]=="teacher" and e.private and e.students:
        ok=[r["user_id"] for r in q("select user_id from class_members where class_id=?",(cid,)).fetchall()]
        mem=[s for s in e.students if s in ok]
        if not mem:
            raise HTTPException(400,"Seçilen öğrenci bu sınıfta değil")
        q("update exams set private=1 where id=?",(eid,))
        for s in mem:
            q("insert or ignore into exam_members values(?,?)",(eid,s))
    return {"id":eid,"code":code}
@app.get("/api/exams")
def exams(u=Depends(me)):
    sweep()
    now=time.time()
    if u["role"]=="teacher":
        rows=q("select e.*,c.name cname,(select count(*) from questions where exam_id=e.id) nq,(select count(*) from attempts a where a.exam_id=e.id) na from exams e left join classes c on c.id=e.class_id where e.owner_id=? and e.scope='class' order by coalesce(e.start_at,e.created) desc",(u["id"],)).fetchall()
    else:
        rows=q("select e.*,c.name cname,t.name teacher,(select count(*) from questions where exam_id=e.id) nq from exams e left join classes c on c.id=e.class_id left join users t on t.id=e.owner_id where (e.owner_id=? and e.scope='self') or (e.scope='class' and ((e.private=0 and e.class_id in(select class_id from class_members where user_id=?)) or e.id in(select exam_id from exam_members where user_id=?))) order by coalesce(e.start_at,e.created) desc",(u["id"],u["id"],u["id"])).fetchall()
    out=[]
    for r in rows:
        d=dict(r)
        d["state"]=estate(r,now)
        if u["role"]!="teacher" or r["owner_id"]!=u["id"]:
            d.pop("code",None)
        if u["role"]!="teacher":
            a=q("select id,status,score,correct,wrong,blank from attempts where exam_id=? and user_id=? order by id desc limit 1",(r["id"],u["id"])).fetchone()
            d["attempt"]=dict(a) if a else None
        out.append(d)
    return {"now":now,"exams":out}
@app.post("/api/exams/join")
def join_exam(c:Code,u=Depends(me)):
    e=q("select * from exams where code=? and guests=1 and scope='class'",(c.code.strip().upper(),)).fetchone()
    if not e:
        raise HTTPException(404,"Geçersiz sınav kodu")
    if u["role"]=="teacher":
        raise HTTPException(403,"Öğretmen sınava giremez")
    if time.time()>e["end_at"]:
        raise HTTPException(400,"Bu sınav kapandı")
    q("insert or ignore into exam_members values(?,?)",(e["id"],u["id"]))
    return {"id":e["id"],"title":e["title"]}
@app.post("/api/exams/{eid}/open-now")
def open_now(eid:int,u=Depends(me)):
    if u["role"]!="teacher":
        raise HTTPException(403,"Yetki yok")
    e=q("select * from exams where id=? and owner_id=? and scope='class'",(eid,u["id"])).fetchone()
    if not e:
        raise HTTPException(404,"Sınav bulunamadı")
    now=time.time()
    if now>=e["end_at"]:
        raise HTTPException(400,"Sınavın bitiş saati geçmiş")
    q("update exams set start_at=? where id=?",(min(e["start_at"],now),eid))
    return {"ok":True}
@app.delete("/api/exams/{eid}")
def del_exam(eid:int,u=Depends(me)):
    if not q("select 1 from exams where id=? and owner_id=?",(eid,u["id"])).fetchone():
        raise HTTPException(404,"Sınav bulunamadı")
    for t in("questions","attempts","exam_members","grades"):
        q(f"delete from {t} where exam_id=?",(eid,))
    q("delete from exams where id=?",(eid,))
    audit(u,"sınav silme",f"sınav {eid}")
    return {"ok":True}
@app.post("/api/exams/{eid}/start")
def start_exam(eid:int,u=Depends(me)):
    sweep()
    e=exam_for(eid,u)
    if u["role"]=="teacher":
        raise HTTPException(403,"Öğretmen sınava giremez")
    now=time.time()
    st=estate(e,now)
    if st=="upcoming":
        raise HTTPException(400,"Sınav henüz başlamadı")
    if st=="closed":
        raise HTTPException(400,"Sınav kapandı")
    a=q("select * from attempts where exam_id=? and user_id=? order by id desc limit 1",(eid,u["id"])).fetchone()
    if a and a["status"]=="devam":
        go_back(a,now)
        a=q("select * from attempts where id=?",(a["id"],)).fetchone()
    elif a and e["scope"]=="class":
        raise HTTPException(409,"Bu sınavı zaten tamamladın")
    else:
        if e["scope"]=="class":
            dl=e["end_at"]
            if e["duration"]:
                dl=min(dl,now+e["duration"]*60)
        else:
            dl=now+(e["duration"] or 360)*60
        ids=[r["id"] for r in q("select id from questions where exam_id=? order by idx",(eid,)).fetchall()]
        if e["shuffle"]:
            random.shuffle(ids)
        aid=q("insert into attempts(exam_id,user_id,started,deadline,ord) values(?,?,?,?,?)",(eid,u["id"],now,dl,json.dumps(ids))).lastrowid
        a=q("select * from attempts where id=?",(aid,)).fetchone()
    qm={r["id"]:r for r in q("select * from questions where exam_id=?",(eid,)).fetchall()}
    return {"aid":a["id"],"title":e["title"],"now":now,"deadline":a["deadline"],"answers":json.loads(a["answers"] or "{}"),"switches":a["switches"],"qs":[{"id":i,"text":qm[i]["q"],"opts":json.loads(qm[i]["opts"])} for i in json.loads(a["ord"]) if i in qm]}
@app.put("/api/attempts/{aid}/answers")
def save_answers(aid:int,b:Ans,u=Depends(me)):
    a=my_attempt(aid,u)
    if a["status"]!="devam" or time.time()>a["deadline"]+3:
        close_attempt(aid)
        raise HTTPException(410,"Süre doldu, sınav kapandı")
    ok={str(r["id"]) for r in q("select id from questions where exam_id=?",(a["exam_id"],)).fetchall()}
    q("update attempts set answers=? where id=?",(json.dumps({k:v for k,v in b.answers.items() if k in ok}),aid))
    return {"ok":True}
@app.post("/api/attempts/{aid}/event")
def attempt_event(aid:int,ev:Ev,u=Depends(me)):
    a=my_attempt(aid,u)
    if a["status"]!="devam":
        return {"ok":True}
    now=time.time()
    if ev.k=="away" and a["away_since"] is None:
        evs=json.loads(a["events"] or "[]")
        evs.append({"k":"away","at":now})
        q("update attempts set away_since=?,switches=switches+1,events=? where id=?",(now,json.dumps(evs[-300:]),aid))
    elif ev.k=="back":
        go_back(a,now)
    elif ev.k=="copy":
        evs=json.loads(a["events"] or "[]")
        evs.append({"k":"copy","at":now})
        q("update attempts set copies=copies+1,events=? where id=?",(json.dumps(evs[-300:]),aid))
    return {"ok":True}
def attempt_result(a,e):
    now=time.time()
    qs=q("select * from questions where exam_id=? order by idx",(e["id"],)).fetchall()
    out={"aid":a["id"],"exam":{"id":e["id"],"title":e["title"],"kind":e["kind"],"scope":e["scope"],"state":estate(e,now)},"score":a["score"],"correct":a["correct"],"wrong":a["wrong"],"blank":a["blank"],"total":len(qs),"switches":a["switches"],"away_sec":a["away_sec"],"prev":prev_for(e,a["user_id"]),"rank":None,"n":None,"review":None}
    if e["scope"]=="class" and out["exam"]["state"]=="closed":
        out["rank"]=q("select count(*)+1 from attempts where exam_id=? and status='bitti' and score>?",(e["id"],a["score"])).fetchone()[0]
        out["n"]=q("select count(*) from attempts where exam_id=? and status='bitti'",(e["id"],)).fetchone()[0]
    if e["scope"]=="self" or out["exam"]["state"]=="closed":
        ans=json.loads(a["answers"] or "{}")
        out["review"]=[{"text":x["q"],"opts":json.loads(x["opts"]),"ans":x["ans"],"mine":ans.get(str(x["id"])),"expl":x["expl"],"topic":x["topic"]} for x in qs]
    return out
@app.post("/api/attempts/{aid}/finish")
def finish_attempt(aid:int,b:Fin,u=Depends(me)):
    a=my_attempt(aid,u)
    if a["status"]=="devam":
        if b.answers is not None and time.time()<=a["deadline"]+10:
            ok={str(r["id"]) for r in q("select id from questions where exam_id=?",(a["exam_id"],)).fetchall()}
            q("update attempts set answers=? where id=?",(json.dumps({k:v for k,v in b.answers.items() if k in ok}),aid))
        close_attempt(aid)
    return get_attempt(aid,u)
@app.get("/api/attempts/{aid}")
def get_attempt(aid:int,u=Depends(me)):
    sweep()
    a=my_attempt(aid,u)
    if a["status"]=="devam":
        raise HTTPException(400,"Sınav hâlâ sürüyor")
    return attempt_result(a,q("select * from exams where id=?",(a["exam_id"],)).fetchone())
@app.get("/api/me/progress")
def my_progress(u=Depends(me)):
    sweep()
    rows=q("select a.id aid,a.exam_id,a.score,a.correct,a.wrong,a.blank,a.finished,a.switches,e.* from attempts a join exams e on e.id=a.exam_id where a.user_id=? and a.status='bitti' order by a.finished",(u["id"],)).fetchall()
    res=[]
    for r in rows:
        e=q("select * from exams where id=?",(r["exam_id"],)).fetchone()
        d={"aid":r["aid"],"exam_id":r["exam_id"],"title":e["title"],"kind":e["kind"],"scope":e["scope"],"score":r["score"],"correct":r["correct"],"wrong":r["wrong"],"blank":r["blank"],"finished":r["finished"],"switches":r["switches"],"prev":prev_for(e,u["id"]),"rank":None,"n":None}
        if e["scope"]=="class" and estate(e)=="closed":
            d["rank"]=q("select count(*)+1 from attempts where exam_id=? and status='bitti' and score>?",(e["id"],r["score"])).fetchone()[0]
            d["n"]=q("select count(*) from attempts where exam_id=? and status='bitti'",(e["id"],)).fetchone()[0]
        res.append(d)
    gr=q("select g.id,g.kind,g.score,g.note,g.at,c.name cname from grades g left join classes c on c.id=g.class_id where g.student_id=? order by g.at desc",(u["id"],)).fetchall()
    return {"results":res,"grades":[dict(g) for g in gr]}
@app.get("/api/exams/{eid}/report")
def exam_report(eid:int,u=Depends(teacher)):
    sweep()
    e=q("select * from exams where id=? and owner_id=?",(eid,u["id"])).fetchone()
    if not e:
        raise HTTPException(404,"Sınav bulunamadı")
    qs=[dict(x) for x in q("select * from questions where exam_id=? order by idx",(eid,)).fetchall()]
    members=[dict(r) for r in q("select u.id,u.name from class_members m join users u on u.id=m.user_id where m.class_id=?",(e["class_id"],)).fetchall()] if e["class_id"] else []
    atts=q("select a.*,us.name,us.role from attempts a join users us on us.id=a.user_id where a.exam_id=? order by a.id",(eid,)).fetchall()
    rows=[]
    for a in atts:
        done=a["status"]=="bitti"
        rows.append({"uid":a["user_id"],"name":a["name"],"guest":a["role"]=="guest","status":a["status"],"correct":a["correct"],"wrong":a["wrong"],"blank":a["blank"],"score":a["score"],"switches":a["switches"],"away_sec":a["away_sec"],"copies":a["copies"],"secs":int(a["finished"]-a["started"]) if done and a["finished"] else None,"events":json.loads(a["events"] or "[]"),"started":a["started"],"prev":prev_for(e,a["user_id"]),"rank":None})
    fin=sorted([r for r in rows if r["status"]=="bitti"],key=lambda r:(-r["score"],r["secs"] or 10**9))
    for r in fin:
        r["rank"]=1+sum(1 for x in fin if x["score"]>r["score"])
    rows.sort(key=lambda r:(r["rank"] is None,r["rank"] or 0,r["name"]))
    seen={r["uid"] for r in rows}
    scores=[r["score"] for r in fin]
    pe=q("select id,title from exams where id!=? and owner_id=? and scope=? and coalesce(class_id,0)=coalesce(?,0) and coalesce(start_at,created)<coalesce(?,?) order by coalesce(start_at,created) desc limit 1",(eid,e["owner_id"],e["scope"],e["class_id"],e["start_at"],e["created"])).fetchone()
    pavg=None
    if pe:
        v=q("select avg(score) from attempts where exam_id=? and status='bitti'",(pe["id"],)).fetchone()[0]
        pavg=round(v,1) if v is not None else None
    qa=[]
    done=[a for a in atts if a["status"]=="bitti"]
    for x in qs:
        o=json.loads(x["opts"])
        cnt=[0]*len(o)
        blank=0
        for a in done:
            v=json.loads(a["answers"] or "{}").get(str(x["id"]))
            if v is None or v<0 or v>=len(o):
                blank+=1
            else:
                cnt[v]+=1
        qa.append({"idx":x["idx"]+1,"text":x["q"],"topic":x["topic"],"opts":o,"ans":x["ans"],"cnt":cnt,"blank":blank,"rate":round(100*cnt[x["ans"]]/len(done)) if done else None})
    return {"exam":{"id":e["id"],"title":e["title"],"kind":e["kind"],"start_at":e["start_at"],"end_at":e["end_at"],"duration":e["duration"],"state":estate(e),"class_id":e["class_id"],"nq":len(qs)},"rows":rows,"absent":[m["name"] for m in members if m["id"] not in seen],"total":len(members),"avg":round(sum(scores)/len(scores),1) if scores else None,"max":max(scores) if scores else None,"min":min(scores) if scores else None,"prev":{"title":pe["title"],"avg":pavg} if pe else None,"qa":qa}
@app.post("/api/exams/{eid}/grades")
def push_grades(eid:int,u=Depends(teacher)):
    sweep()
    e=q("select * from exams where id=? and owner_id=?",(eid,u["id"])).fetchone()
    if not e:
        raise HTTPException(404,"Sınav bulunamadı")
    if not e["class_id"]:
        raise HTTPException(400,"Bu sınav bir sınıfa bağlı değil")
    k="yazili" if e["kind"]=="sinav" else "deneme"
    q("delete from grades where exam_id=?",(eid,))
    n=0
    for a in q("select a.user_id,a.score from attempts a join class_members m on m.user_id=a.user_id and m.class_id=? where a.exam_id=? and a.status='bitti'",(e["class_id"],eid)).fetchall():
        q("insert into grades(class_id,student_id,teacher_id,kind,score,note,at,exam_id) values(?,?,?,?,?,?,?,?)",(e["class_id"],a["user_id"],u["id"],k,a["score"],e["title"][:100],time.time(),eid))
        n+=1
    return {"n":n}

# =====================================================================
#  ANALİZ ve ÖĞRETMEN REHBERİ
# =====================================================================
def build_stats(cid):
    sweep()
    now=time.time()
    mem=q("select u.id,u.name from class_members m join users u on u.id=m.user_id where m.class_id=? order by u.name",(cid,)).fetchall()
    exs=q("select * from exams where class_id=? and scope='class' and end_at<? order by start_at",(cid,now)).fetchall()
    lessons=[r["id"] for r in q("select id from lessons where class_id=? order by id",(cid,)).fetchall()]
    qinfo={e["id"]:{x["id"]:(x["ans"],x["topic"]) for x in q("select id,ans,topic from questions where exam_id=?",(e["id"],)).fetchall()} for e in exs}
    atts={}
    trend=[]
    for e in exs:
        rs=q("select * from attempts where exam_id=? and status='bitti'",(e["id"],)).fetchall()
        for a in rs:
            atts[(e["id"],a["user_id"])]=a
        if not e["private"]:
          trend.append({"id":e["id"],"title":e["title"],"kind":e["kind"],"n":len(rs),"avg":round(sum(a["score"] for a in rs)/len(rs),1) if rs else None})
    ctop={}
    students=[]
    for m in mem:
        uid=m["id"]
        sc=[]
        tt={}
        sw=aw=mx=0
        for e in exs:
            a=atts.get((e["id"],uid))
            if not a:
                continue
            sc.append({"id":e["id"],"title":e["title"],"score":a["score"]})
            sw+=a["switches"]
            aw+=a["away_sec"]
            mx=max(mx,a["switches"])
            ans=json.loads(a["answers"] or "{}")
            for qid,(ca,tp) in qinfo[e["id"]].items():
                if not tp:
                    continue
                ok=1 if ans.get(str(qid))==ca else 0
                for d in(tt,ctop):
                    t=d.setdefault(tp,[0,0])
                    t[0]+=ok
                    t[1]+=1
        avg=round(sum(x["score"] for x in sc)/len(sc),1) if sc else None
        delta=None
        tr="na"
        if len(sc)>=2:
            base=sum(x["score"] for x in sc[:-1])/len(sc[:-1])
            delta=round(sc[-1]["score"]-base,1)
            tr="up" if delta>=5 else "down" if delta<=-5 else "flat"
        ph=",".join("?"*len(lessons)) or "null"
        at=q(f"select count(*) n,coalesce(sum(secs),0) s,coalesce(sum(hands),0) h,coalesce(sum(chats),0) c from attendance where user_id=? and lesson_id in({ph})",(uid,*lessons)).fetchone()
        rate=round(at["n"]/len(lessons),2) if lessons else None
        weak=[k for r,k in sorted((v[0]/v[1],k) for k,v in tt.items() if v[1]>=2 and v[0]/v[1]<.5)[:3]]
        gr={r["kind"]:round(r["a"],1) for r in q("select kind,avg(score) a from grades where class_id=? and student_id=? group by kind",(cid,uid)).fetchall()}
        fl=[]
        if tr=="down" and delta<=-10:
            fl.append({"t":"warn","m":f"Başarı düşüşte (son sınav ortalamasının {abs(delta)} puan altında)"})
        att_ids={r["lesson_id"] for r in q("select lesson_id from attendance where user_id=?",(uid,)).fetchall()}
        streak=0
        for lid in reversed(lessons):
            if lid in att_ids:
                break
            streak+=1
        if streak>=3:
            fl.append({"t":"warn","m":f"Son {streak} dersin hiçbirine katılmadı"})
        if rate is not None and len(lessons)>=2 and rate<.6:
            fl.append({"t":"warn","m":f"Derse katılımı düşük (%{round(rate*100)})"})
        if avg is not None and avg<50:
            fl.append({"t":"warn","m":f"Sınav ortalaması düşük ({avg})"})
            if rate is not None and rate>=.8:
                fl.append({"t":"warn","m":"Derse düzenli geliyor ama başarısı düşük: farklı anlatım ya da ek destek düşünülebilir"})
        if mx>=5:
            fl.append({"t":"info","m":f"Bir sınavda ekranı {mx} kez değiştirdi (kopya kanıtı değildir; dikkat/ortam sorunu olabilir)"})
        if exs and len(sc)<len(exs):
            fl.append({"t":"warn","m":f"{len(exs)-len(sc)} sınava girmedi"})
        if tr=="up" and delta>=10:
            fl.append({"t":"good","m":f"Yükselişte (+{delta} puan)"})
        students.append({"id":uid,"name":m["name"],"scores":sc,"avg":avg,"trend":tr,"delta":delta,"taken":len(sc),"lessons_attended":at["n"],"part_rate":rate,"mins":round(at["s"]/60),"hands":at["h"],"chats":at["c"],"switches":sw,"away_sec":aw,"weak":weak,"grades":gr,"flags":fl})
    topics=[{"topic":k,"rate":round(100*v[0]/v[1]),"n":v[1]} for k,v in sorted(ctop.items(),key=lambda kv:kv[1][0]/kv[1][1]) if v[1]>=5][:6]
    return {"students":students,"trend":trend,"topics":topics,"lessons":len(lessons),"exams":len(exs)}
@app.get("/api/classes/{cid}/analytics")
def analytics(cid:int,u=Depends(teacher)):
    own_class(cid,u)
    return build_stats(cid)
def fallback_guide(st):
    L=["**Genel durum**"]
    tr=[t for t in st["trend"] if t["avg"] is not None]
    if tr:
        L.append("Sınıf sınav ortalamaları: "+", ".join(f"{t['title']}: {t['avg']}" for t in tr[-4:])+".")
    else:
        L.append("Henüz kapanmış ve değerlendirilmiş sınıf sınavı yok.")
    if st["topics"]:
        L.append("")
        L.append("**Zayıf konular (tekrar önerilir)**")
        L+=[f"- {t['topic']}: %{t['rate']} doğru ({t['n']} cevap)" for t in st["topics"][:5]]
    warn=[(s["name"],f["m"]) for s in st["students"] for f in s["flags"] if f["t"]=="warn"]
    if warn:
        L.append("")
        L.append("**Dikkat gerektiren öğrenciler**")
        L+=[f"- {n}: {m}" for n,m in warn]
    good=[(s["name"],f["m"]) for s in st["students"] for f in s["flags"] if f["t"]=="good"]
    if good:
        L.append("")
        L.append("**Yükselişte olanlar**")
        L+=[f"- {n}: {m}" for n,m in good]
    L.append("")
    L.append("_Bu özet kural tabanlıdır. Daha ayrıntılı öneri için Ayarlar'dan yapay zeka anahtarı gir._")
    return "\n".join(L)
@app.post("/api/classes/{cid}/guide")
def class_guide(cid:int,u=Depends(teacher)):
    own_class(cid,u)
    st=build_stats(cid)
    if not st["students"]:
        raise HTTPException(400,"Sınıfta öğrenci yok")
    lab={s["id"]:f"Öğrenci {i+1}" for i,s in enumerate(st["students"])}
    data={"sinif_sinav_ortalamalari":[{"sinav":t["title"],"ortalama":t["avg"],"katilan":t["n"]} for t in st["trend"]],"sinif_zayif_konular":st["topics"],"toplam_ders":st["lessons"],"toplam_sinav":st["exams"],"ogrenciler":[{"ad":lab[s["id"]],"ortalama":s["avg"],"egilim":s["trend"],"son_fark":s["delta"],"girilen_sinav":s["taken"],"derse_katilim_orani":s["part_rate"],"el_kaldirma":s["hands"],"sohbet_mesaji":s["chats"],"ekran_degistirme_toplam":s["switches"],"zayif_konular":s["weak"],"notlar":s["grades"],"uyarilar":[f["m"] for f in s["flags"]]} for s in st["students"]]}
    prompt=f"""Sen deneyimli bir eğitim danışmanısın ve bir sınıf öğretmenine yardım ediyorsun.
Aşağıda bir sınıfın sınav sonuçları, derse katılım ve ekran değiştirme verileri var. Öğrenciler anonimdir ("Öğrenci 1" gibi).
Bu verilere dayanarak Türkçe, kısa ve uygulanabilir bir ÖĞRETMEN REHBERİ yaz. Şu başlıkları kullan (her başlığı **kalın** yaz):
**Genel durum**, **Dikkat gerektiren öğrenciler** (neden + ne yapılabilir), **Zayıf konular ve tekrar önerisi**, **Önümüzdeki 2 hafta için somut öneriler**, **Yükselişte olanlar**.
Kurallar: Ekran değiştirme sayısı kopya kanıtı DEĞİLDİR, yalnızca dikkat/ortam göstergesi olarak yorumla. Verilerde olmayan bir şeyi uydurma. Tanı koyma, etiketleme yapma. Veri azsa bunu açıkça söyle.
VERİ (JSON):
{json.dumps(data,ensure_ascii=False)}"""
    try:
        txt=ai_text(u,prompt)
        ai,err=True,""
        txt=re.sub(r"Öğrenci (\d+)",lambda m:st["students"][int(m[1])-1]["name"] if 0<int(m[1])<=len(st["students"]) else m[0],txt)
    except HTTPException as e:
        txt,ai,err=fallback_guide(st),False,str(e.detail)
    return {"guide":txt,"ai":ai,"error":err}

@app.get("/")
def index():
    return FileResponse("index.html")
