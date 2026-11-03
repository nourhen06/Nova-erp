
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
import os
from io import BytesIO
import sqlite3

BASE=Path(__file__).resolve().parent
DB=Path(os.environ.get("NOVA_DB_PATH", str(BASE/"nova.db")))
app=FastAPI(title="NOVA ERP - prototype local")
app.mount("/static",StaticFiles(directory=BASE/"static"),name="static")

def connect():
    c=sqlite3.connect(DB)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init():
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS clients(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT DEFAULT '', phone TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, name TEXT NOT NULL, price_cents INTEGER NOT NULL CHECK(price_cents>=0), stock INTEGER NOT NULL DEFAULT 0 CHECK(stock>=0));
        CREATE TABLE IF NOT EXISTS invoices(id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, client_id INTEGER NOT NULL REFERENCES clients(id), created TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','validated','paid')), subtotal_cents INTEGER NOT NULL, vat_cents INTEGER NOT NULL, total_cents INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS invoice_lines(id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id), product_id INTEGER NOT NULL REFERENCES products(id), description TEXT NOT NULL, qty INTEGER NOT NULL CHECK(qty>0), unit_cents INTEGER NOT NULL, vat_rate INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id), account TEXT NOT NULL, debit_cents INTEGER NOT NULL DEFAULT 0, credit_cents INTEGER NOT NULL DEFAULT 0);
        """)
init()
class Client(BaseModel):
    name:str=Field(min_length=1,max_length=200)
    email:str=""
    phone:str=""
class Product(BaseModel):
    name:str=Field(min_length=1,max_length=200)
    price:Decimal=Field(ge=0)
    stock:int=Field(ge=0)
class Line(BaseModel):
    product_id:int
    qty:int=Field(gt=0)
    vat_rate:int=Field(default=20,ge=0,le=100)
class Invoice(BaseModel):
    client_id:int
    lines:list[Line]=Field(min_length=1)

def cents(x):
    return int((Decimal(str(x))*100).quantize(Decimal("1"),rounding=ROUND_HALF_UP))
def records(query,params=()):
    with connect() as c: return [dict(r) for r in c.execute(query,params).fetchall()]
@app.get("/")
def home(): return FileResponse(BASE/"static"/"index.html")
@app.get("/api/clients")
def clients(): return records("SELECT * FROM clients ORDER BY id DESC")
@app.post("/api/clients")
def add_client(x:Client):
    with connect() as c:
        cur=c.execute("INSERT INTO clients(name,email,phone) VALUES(?,?,?)",(x.name,x.email,x.phone))
        return {"id":cur.lastrowid}
@app.get("/api/products")
def products(): return records("SELECT * FROM products ORDER BY id DESC")
@app.post("/api/products")
def add_product(x:Product):
    with connect() as c:
        cur=c.execute("INSERT INTO products(name,price_cents,stock) VALUES(?,?,?)",(x.name,cents(x.price),x.stock))
        return {"id":cur.lastrowid}
@app.get("/api/invoices")
def invoices():
    return records("""SELECT i.*,c.name client FROM invoices i JOIN clients c ON c.id=i.client_id ORDER BY i.id DESC""")
@app.post("/api/invoices")
def add_invoice(x:Invoice):
    with connect() as c:
        if not c.execute("SELECT 1 FROM clients WHERE id=?",(x.client_id,)).fetchone(): raise HTTPException(400,"Client inconnu")
        prepared=[]; subtotal=vat=0
        for line in x.lines:
            p=c.execute("SELECT * FROM products WHERE id=?",(line.product_id,)).fetchone()
            if not p: raise HTTPException(400,"Produit inconnu")
            amount=p["price_cents"]*line.qty
            tax=(amount*line.vat_rate+50)//100
            subtotal+=amount;vat+=tax
            prepared.append((p["id"],p["name"],line.qty,p["price_cents"],line.vat_rate))
        next_id=c.execute("SELECT COALESCE(MAX(id),0)+1 FROM invoices").fetchone()[0]
        number=f"FAC-{date.today().year}-{next_id:05d}"
        cur=c.execute("INSERT INTO invoices(number,client_id,created,subtotal_cents,vat_cents,total_cents) VALUES(?,?,?,?,?,?)",(number,x.client_id,date.today().isoformat(),subtotal,vat,subtotal+vat))
        for p in prepared: c.execute("INSERT INTO invoice_lines(invoice_id,product_id,description,qty,unit_cents,vat_rate) VALUES(?,?,?,?,?,?)",(cur.lastrowid,*p))
        return {"id":cur.lastrowid,"number":number}
@app.get("/api/invoices/{invoice_id}")
def invoice_detail(invoice_id:int):
    inv=records("SELECT i.*,c.name client FROM invoices i JOIN clients c ON c.id=i.client_id WHERE i.id=?",(invoice_id,))
    if not inv: raise HTTPException(404,"Facture introuvable")
    inv[0]["lines"]=records("SELECT * FROM invoice_lines WHERE invoice_id=?",(invoice_id,))
    return inv[0]
@app.post("/api/invoices/{invoice_id}/validate")
def validate(invoice_id:int):
    with connect() as c:
        i=c.execute("SELECT * FROM invoices WHERE id=?",(invoice_id,)).fetchone()
        if not i: raise HTTPException(404,"Facture introuvable")
        if i["status"]!="draft": raise HTTPException(400,"Facture déjà validée")
        c.execute("UPDATE invoices SET status='validated' WHERE id=?",(invoice_id,))
        for account,debit,credit in [("411",i["total_cents"],0),("707",0,i["subtotal_cents"]),("44571",0,i["vat_cents"])]:
            if debit or credit: c.execute("INSERT INTO entries(invoice_id,account,debit_cents,credit_cents) VALUES(?,?,?,?)",(invoice_id,account,debit,credit))
    return {"status":"validated"}
@app.post("/api/invoices/{invoice_id}/pay")
def pay(invoice_id:int):
    with connect() as c:
        i=c.execute("SELECT * FROM invoices WHERE id=?",(invoice_id,)).fetchone()
        if not i: raise HTTPException(404,"Facture introuvable")
        if i["status"]!="validated": raise HTTPException(400,"Facture non validée ou déjà payée")
        c.execute("UPDATE invoices SET status='paid' WHERE id=?",(invoice_id,))
        for account,debit,credit in [("512",i["total_cents"],0),("411",0,i["total_cents"])]:
            c.execute("INSERT INTO entries(invoice_id,account,debit_cents,credit_cents) VALUES(?,?,?,?)",(invoice_id,account,debit,credit))
    return {"status":"paid"}
@app.get("/api/entries")
def entries(): return records("SELECT e.*,i.number FROM entries e JOIN invoices i ON i.id=e.invoice_id ORDER BY e.id DESC")
@app.get("/api/dashboard")
def dashboard():
    with connect() as c:
        return {"clients":c.execute("SELECT COUNT(*) FROM clients").fetchone()[0],
                "products":c.execute("SELECT COUNT(*) FROM products").fetchone()[0],
                "invoices":c.execute("SELECT COUNT(*) FROM invoices").fetchone()[0],
                "revenue_cents":c.execute("SELECT COALESCE(SUM(subtotal_cents),0) FROM invoices WHERE status IN ('validated','paid')").fetchone()[0],
                "unpaid_cents":c.execute("SELECT COALESCE(SUM(total_cents),0) FROM invoices WHERE status='validated'").fetchone()[0]}
@app.get("/api/invoices/{invoice_id}/pdf")
def invoice_pdf(invoice_id:int):
    from reportlab.pdfgen import canvas
    i=invoice_detail(invoice_id)
    b=BytesIO();p=canvas.Canvas(b,pagesize=(595,842))
    p.setFont("Helvetica-Bold",22);p.drawString(48,785,"NOVA ERP | FACTURE")
    p.setFont("Helvetica",11)
    p.drawString(48,748,f"Numero : {i['number']}")
    p.drawString(48,730,f"Date : {i['created']}")
    p.drawString(48,712,f"Client : {i['client']}")
    y=668
    for line in i["lines"]:
        p.drawString(48,y,f"{line['description']} - {line['qty']} x {line['unit_cents']/100:.2f} EUR - TVA {line['vat_rate']}%")
        y-=24
        if y<150: p.showPage();y=780
    p.drawString(48,y-30,f"HT : {i['subtotal_cents']/100:.2f} EUR")
    p.drawString(48,y-50,f"TVA : {i['vat_cents']/100:.2f} EUR")
    p.setFont("Helvetica-Bold",13);p.drawString(48,y-75,f"TTC : {i['total_cents']/100:.2f} EUR")
    p.setFont("Helvetica",9);p.drawString(48,55,"Document de demonstration : mentions legales de l'emetteur a configurer avant utilisation reelle.")
    p.save();b.seek(0)
    return StreamingResponse(b,media_type="application/pdf",headers={"Content-Disposition":f'inline; filename="{i["number"]}.pdf"'})


# --- NOVA ERP v0.2 : fournisseurs, achats, devis, mouvements de stocks ---
class Supplier(BaseModel):
    name:str=Field(min_length=1,max_length=200)
    email:str=""
class StockMove(BaseModel):
    product_id:int
    qty:int=Field(gt=0)
    direction:str
    note:str=""
class Purchase(BaseModel):
    supplier_id:int
    product_id:int
    qty:int=Field(gt=0)
    unit_price:Decimal=Field(ge=0)
class Quote(BaseModel):
    client_id:int
    product_id:int
    qty:int=Field(gt=0)
    vat_rate:int=Field(default=20,ge=0,le=100)

def init_v2():
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS suppliers(id INTEGER PRIMARY KEY,name TEXT NOT NULL,email TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS stock_moves(id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL REFERENCES products(id),qty INTEGER NOT NULL,reason TEXT NOT NULL,created TEXT NOT NULL,note TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS purchases(id INTEGER PRIMARY KEY,number TEXT UNIQUE NOT NULL,supplier_id INTEGER NOT NULL REFERENCES suppliers(id),product_id INTEGER NOT NULL REFERENCES products(id),qty INTEGER NOT NULL,unit_cents INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'ordered' CHECK(status IN ('ordered','received')),created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS quotes(id INTEGER PRIMARY KEY,number TEXT UNIQUE NOT NULL,client_id INTEGER NOT NULL REFERENCES clients(id),product_id INTEGER NOT NULL REFERENCES products(id),qty INTEGER NOT NULL,unit_cents INTEGER NOT NULL,vat_rate INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','converted')),invoice_id INTEGER REFERENCES invoices(id),created TEXT NOT NULL);
        ''')
init_v2()
@app.get('/api/suppliers')
def suppliers(): return records('SELECT * FROM suppliers ORDER BY id DESC')
@app.post('/api/suppliers')
def add_supplier(x:Supplier):
    with connect() as c:
        cur=c.execute('INSERT INTO suppliers(name,email) VALUES(?,?)',(x.name,x.email))
        return {'id':cur.lastrowid}
@app.get('/api/stock-moves')
def stock_moves():
    return records('SELECT m.*,p.name product FROM stock_moves m JOIN products p ON p.id=m.product_id ORDER BY m.id DESC')
@app.post('/api/stock-moves')
def add_stock_move(x:StockMove):
    if x.direction not in ('in','out'): raise HTTPException(400,'Sens invalide')
    with connect() as c:
        p=c.execute('SELECT * FROM products WHERE id=?',(x.product_id,)).fetchone()
        if not p: raise HTTPException(404,'Produit introuvable')
        delta=x.qty if x.direction=='in' else -x.qty
        if p['stock']+delta<0: raise HTTPException(400,'Stock insuffisant')
        c.execute('UPDATE products SET stock=stock+? WHERE id=?',(delta,x.product_id))
        cur=c.execute('INSERT INTO stock_moves(product_id,qty,reason,created,note) VALUES(?,?,?,?,?)',(x.product_id,delta,'manual',date.today().isoformat(),x.note))
        return {'id':cur.lastrowid,'stock':p['stock']+delta}
@app.get('/api/purchases')
def purchases():
    return records('''SELECT a.*,s.name supplier,p.name product FROM purchases a JOIN suppliers s ON s.id=a.supplier_id JOIN products p ON p.id=a.product_id ORDER BY a.id DESC''')
@app.post('/api/purchases')
def add_purchase(x:Purchase):
    with connect() as c:
        if not c.execute('SELECT 1 FROM suppliers WHERE id=?',(x.supplier_id,)).fetchone(): raise HTTPException(400,'Fournisseur inconnu')
        if not c.execute('SELECT 1 FROM products WHERE id=?',(x.product_id,)).fetchone(): raise HTTPException(400,'Produit inconnu')
        n=c.execute('SELECT COALESCE(MAX(id),0)+1 FROM purchases').fetchone()[0]
        cur=c.execute('INSERT INTO purchases(number,supplier_id,product_id,qty,unit_cents,created) VALUES(?,?,?,?,?,?)',(f'ACH-{date.today().year}-{n:05d}',x.supplier_id,x.product_id,x.qty,cents(x.unit_price),date.today().isoformat()))
        return {'id':cur.lastrowid}
@app.post('/api/purchases/{purchase_id}/receive')
def receive_purchase(purchase_id:int):
    with connect() as c:
        x=c.execute('SELECT * FROM purchases WHERE id=?',(purchase_id,)).fetchone()
        if not x: raise HTTPException(404,'Achat introuvable')
        if x['status']!='ordered': raise HTTPException(400,'Achat déjà réceptionné')
        c.execute("UPDATE purchases SET status='received' WHERE id=?",(purchase_id,))
        c.execute('UPDATE products SET stock=stock+? WHERE id=?',(x['qty'],x['product_id']))
        c.execute('INSERT INTO stock_moves(product_id,qty,reason,created,note) VALUES(?,?,?,?,?)',(x['product_id'],x['qty'],'purchase',date.today().isoformat(),x['number']))
        return {'status':'received'}
@app.get('/api/quotes')
def quotes():
    return records('''SELECT q.*,c.name client,p.name product FROM quotes q JOIN clients c ON c.id=q.client_id JOIN products p ON p.id=q.product_id ORDER BY q.id DESC''')
@app.post('/api/quotes')
def add_quote(x:Quote):
    with connect() as c:
        if not c.execute('SELECT 1 FROM clients WHERE id=?',(x.client_id,)).fetchone(): raise HTTPException(400,'Client inconnu')
        p=c.execute('SELECT * FROM products WHERE id=?',(x.product_id,)).fetchone()
        if not p: raise HTTPException(400,'Produit inconnu')
        n=c.execute('SELECT COALESCE(MAX(id),0)+1 FROM quotes').fetchone()[0]
        cur=c.execute('INSERT INTO quotes(number,client_id,product_id,qty,unit_cents,vat_rate,created) VALUES(?,?,?,?,?,?,?)',(f'DEV-{date.today().year}-{n:05d}',x.client_id,x.product_id,x.qty,p['price_cents'],x.vat_rate,date.today().isoformat()))
        return {'id':cur.lastrowid}
@app.post('/api/quotes/{quote_id}/convert')
def convert_quote(quote_id:int):
    with connect() as c:
        q=c.execute('SELECT * FROM quotes WHERE id=?',(quote_id,)).fetchone()
        if not q: raise HTTPException(404,'Devis introuvable')
        if q['status']!='draft': raise HTTPException(400,'Devis déjà converti')
        subtotal=q['qty']*q['unit_cents']; vat=(subtotal*q['vat_rate']+50)//100
        n=c.execute('SELECT COALESCE(MAX(id),0)+1 FROM invoices').fetchone()[0]
        number=f'FAC-{date.today().year}-{n:05d}'
        cur=c.execute('INSERT INTO invoices(number,client_id,created,subtotal_cents,vat_cents,total_cents) VALUES(?,?,?,?,?,?)',(number,q['client_id'],date.today().isoformat(),subtotal,vat,subtotal+vat))
        p=c.execute('SELECT name FROM products WHERE id=?',(q['product_id'],)).fetchone()
        c.execute('INSERT INTO invoice_lines(invoice_id,product_id,description,qty,unit_cents,vat_rate) VALUES(?,?,?,?,?,?)',(cur.lastrowid,q['product_id'],p['name'],q['qty'],q['unit_cents'],q['vat_rate']))
        c.execute("UPDATE quotes SET status='converted',invoice_id=? WHERE id=?",(cur.lastrowid,quote_id))
        return {'invoice_id':cur.lastrowid,'number':number}


# --- NOVA ERP 0.3: fabrication, budgets, projets, RH, banque et aide locale ---
class BillOfMaterials(BaseModel):
    finished_product_id:int
    component_product_id:int
    component_qty:int=Field(gt=0)
class ManufacturingOrder(BaseModel):
    product_id:int
    qty:int=Field(gt=0)
class Budget(BaseModel):
    name:str=Field(min_length=1)
    amount:Decimal=Field(ge=0)
class Project(BaseModel):
    name:str=Field(min_length=1)
    client_id:int|None=None
class Task(BaseModel):
    project_id:int
    title:str=Field(min_length=1)
class Employee(BaseModel):
    name:str=Field(min_length=1)
    job_title:str=''
class BankTransaction(BaseModel):
    label:str=Field(min_length=1)
    amount:Decimal
class AssistantQuestion(BaseModel):
    question:str=Field(min_length=1,max_length=500)

def init_v3():
    with connect() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS bom(id INTEGER PRIMARY KEY,finished_product_id INTEGER NOT NULL REFERENCES products(id),component_product_id INTEGER NOT NULL REFERENCES products(id),component_qty INTEGER NOT NULL CHECK(component_qty>0),UNIQUE(finished_product_id,component_product_id));
        CREATE TABLE IF NOT EXISTS manufacturing(id INTEGER PRIMARY KEY,number TEXT UNIQUE NOT NULL,product_id INTEGER NOT NULL REFERENCES products(id),qty INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'planned',created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS budgets(id INTEGER PRIMARY KEY,name TEXT NOT NULL,amount_cents INTEGER NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY,name TEXT NOT NULL,client_id INTEGER REFERENCES clients(id),created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),title TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'todo');
        CREATE TABLE IF NOT EXISTS employees(id INTEGER PRIMARY KEY,name TEXT NOT NULL,job_title TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS bank_transactions(id INTEGER PRIMARY KEY,label TEXT NOT NULL,amount_cents INTEGER NOT NULL,created TEXT NOT NULL);
        ''')
init_v3()
@app.get('/api/bom')
def get_bom():
    return records('SELECT b.*,p.name finished,c.name component FROM bom b JOIN products p ON p.id=b.finished_product_id JOIN products c ON c.id=b.component_product_id ORDER BY b.id DESC')
@app.post('/api/bom')
def add_bom(x:BillOfMaterials):
    if x.finished_product_id==x.component_product_id: raise HTTPException(400,'Un produit ne peut être son propre composant')
    with connect() as c:
        try:
            cur=c.execute('INSERT INTO bom(finished_product_id,component_product_id,component_qty) VALUES(?,?,?)',(x.finished_product_id,x.component_product_id,x.component_qty))
        except sqlite3.IntegrityError: raise HTTPException(400,'Produits inexistants ou composant déjà présent')
        return {'id':cur.lastrowid}
@app.get('/api/manufacturing')
def get_manufacturing():
    return records('SELECT m.*,p.name product FROM manufacturing m JOIN products p ON p.id=m.product_id ORDER BY m.id DESC')
@app.post('/api/manufacturing')
def add_manufacturing(x:ManufacturingOrder):
    with connect() as c:
        if not c.execute('SELECT 1 FROM products WHERE id=?',(x.product_id,)).fetchone(): raise HTTPException(400,'Produit inconnu')
        if not c.execute('SELECT 1 FROM bom WHERE finished_product_id=?',(x.product_id,)).fetchone(): raise HTTPException(400,'Définir une nomenclature avant fabrication')
        n=c.execute('SELECT COALESCE(MAX(id),0)+1 FROM manufacturing').fetchone()[0]
        cur=c.execute('INSERT INTO manufacturing(number,product_id,qty,created) VALUES(?,?,?,?)',(f'OF-{date.today().year}-{n:05d}',x.product_id,x.qty,date.today().isoformat()))
        return {'id':cur.lastrowid}
@app.post('/api/manufacturing/{order_id}/complete')
def complete_manufacturing(order_id:int):
    with connect() as c:
        order=c.execute('SELECT * FROM manufacturing WHERE id=?',(order_id,)).fetchone()
        if not order: raise HTTPException(404,'Ordre introuvable')
        if order['status']!='planned': raise HTTPException(400,'Ordre déjà terminé')
        components=c.execute('SELECT b.component_product_id,b.component_qty,p.stock FROM bom b JOIN products p ON p.id=b.component_product_id WHERE b.finished_product_id=?',(order['product_id'],)).fetchall()
        if not components: raise HTTPException(400,'Nomenclature manquante')
        for comp in components:
            if comp['stock']<comp['component_qty']*order['qty']: raise HTTPException(400,'Stock composant insuffisant')
        for comp in components:
            q=comp['component_qty']*order['qty']
            c.execute('UPDATE products SET stock=stock-? WHERE id=?',(q,comp['component_product_id']))
            c.execute('INSERT INTO stock_moves(product_id,qty,reason,created,note) VALUES(?,?,?,?,?)',(comp['component_product_id'],-q,'manufacturing',date.today().isoformat(),order['number']))
        c.execute('UPDATE products SET stock=stock+? WHERE id=?',(order['qty'],order['product_id']))
        c.execute('INSERT INTO stock_moves(product_id,qty,reason,created,note) VALUES(?,?,?,?,?)',(order['product_id'],order['qty'],'manufacturing',date.today().isoformat(),order['number']))
        c.execute("UPDATE manufacturing SET status='completed' WHERE id=?",(order_id,))
        return {'status':'completed'}
@app.get('/api/budgets')
def get_budgets():return records('SELECT * FROM budgets ORDER BY id DESC')
@app.post('/api/budgets')
def add_budget(x:Budget):
    with connect() as c:
        cur=c.execute('INSERT INTO budgets(name,amount_cents,created) VALUES(?,?,?)',(x.name,cents(x.amount),date.today().isoformat()))
        return {'id':cur.lastrowid}
@app.get('/api/projects')
def get_projects():return records('SELECT p.*,c.name client FROM projects p LEFT JOIN clients c ON c.id=p.client_id ORDER BY p.id DESC')
@app.post('/api/projects')
def add_project(x:Project):
    with connect() as c:
        try:cur=c.execute('INSERT INTO projects(name,client_id,created) VALUES(?,?,?)',(x.name,x.client_id,date.today().isoformat()))
        except sqlite3.IntegrityError:raise HTTPException(400,'Client inconnu')
        return {'id':cur.lastrowid}
@app.get('/api/tasks')
def get_tasks():return records('SELECT t.*,p.name project FROM tasks t JOIN projects p ON p.id=t.project_id ORDER BY t.id DESC')
@app.post('/api/tasks')
def add_task(x:Task):
    with connect() as c:
        try:cur=c.execute('INSERT INTO tasks(project_id,title) VALUES(?,?)',(x.project_id,x.title))
        except sqlite3.IntegrityError:raise HTTPException(400,'Projet inconnu')
        return {'id':cur.lastrowid}
@app.post('/api/tasks/{task_id}/complete')
def complete_task(task_id:int):
    with connect() as c:
        cur=c.execute("UPDATE tasks SET status='done' WHERE id=? AND status='todo'",(task_id,))
        if cur.rowcount!=1:raise HTTPException(400,'Tâche introuvable ou terminée')
        return {'status':'done'}
@app.get('/api/employees')
def get_employees():return records('SELECT * FROM employees ORDER BY id DESC')
@app.post('/api/employees')
def add_employee(x:Employee):
    with connect() as c:
        cur=c.execute('INSERT INTO employees(name,job_title) VALUES(?,?)',(x.name,x.job_title))
        return {'id':cur.lastrowid}
@app.get('/api/bank')
def get_bank():return records('SELECT * FROM bank_transactions ORDER BY id DESC')
@app.post('/api/bank')
def add_bank(x:BankTransaction):
    with connect() as c:
        cur=c.execute('INSERT INTO bank_transactions(label,amount_cents,created) VALUES(?,?,?)',(x.label,cents(x.amount),date.today().isoformat()))
        return {'id':cur.lastrowid}
@app.get('/api/finance')
def finance():
    with connect() as c:
        bank=c.execute('SELECT COALESCE(SUM(amount_cents),0) FROM bank_transactions').fetchone()[0]
        budget=c.execute('SELECT COALESCE(SUM(amount_cents),0) FROM budgets').fetchone()[0]
        return {'bank_balance_cents':bank,'budget_total_cents':budget,'note':'Banque saisie manuellement, sans rapprochement automatique'}
@app.post('/api/assistant')
def assistant(x:AssistantQuestion):
    q=x.question.lower()
    d=dashboard();f=finance()
    if any(w in q for w in ['impay','encaisser','reste à payer']): answer=f"Factures validées non encaissées : {d['unpaid_cents']/100:.2f} € TTC."
    elif any(w in q for w in ['chiffre','vente','ca ']): answer=f"Chiffre d’affaires HT des factures validées : {d['revenue_cents']/100:.2f} €."
    elif any(w in q for w in ['stock','produit']): answer=f"{d['products']} produits enregistrés. Consultez le module Produits pour les quantités."
    elif any(w in q for w in ['client']): answer=f"{d['clients']} clients enregistrés dans NOVA ERP."
    elif any(w in q for w in ['banque','trésorerie']): answer=f"Solde des mouvements bancaires saisis : {f['bank_balance_cents']/100:.2f} €."
    else: answer="Je peux répondre sur les clients, les stocks, le chiffre d’affaires, les factures à encaisser et la banque."
    return {'answer':answer,'engine':'assistant local à règles — pas une IA générative'}


# --- NOVA ERP v0.4 : pilotage / alertes / aide à la décision ---
@app.get('/api/alerts')
def alerts():
    from datetime import date
    from datetime import time
    today=date.today()
    out=[]
    with connect() as c:
        for r in c.execute("SELECT i.id,i.number,i.created,i.total_cents,c.name client FROM invoices i JOIN clients c ON c.id=i.client_id WHERE i.status='validated' ORDER BY i.created ASC"):
            days=(today-date.fromisoformat(r['created'])).days
            if days>=30:
                out.append({'key':f'invoice-{r["id"]}','severity':'high','category':'Facturation','title':'Facture non encaissée depuis 30 jours ou plus','detail':f'{r["number"]} · {r["client"]} · {r["total_cents"]/100:.2f} € · {days} jours depuis la création. Attention : il ne s’agit pas nécessairement d’un retard contractuel (aucune échéance n’est enregistrée).','impact':'Créance à surveiller, risque pour les encaissements.','options':[{'label':'Vérifier la date d’échéance et contacter le client','tradeoff':'Permet de confirmer le retard avant une relance.'},{'label':'Proposer un échéancier après échange','tradeoff':'Facilite le recouvrement, mais reporte la trésorerie.'}],'action':'Ouvrir la facture','module':'invoices','priority':3})
        for r in c.execute('SELECT id,name,stock FROM products WHERE stock<=5 ORDER BY stock ASC'):
            out.append({'key':f'stock-{r["id"]}','severity':'high' if r['stock']==0 else 'medium','category':'Stocks','title':'Rupture de stock' if r['stock']==0 else 'Stock faible','detail':f'{r["name"]} : {r["stock"]} unité(s) disponible(s). Seuil de démonstration : 5 unités.','impact':'Risque de ne pas pouvoir honorer une prochaine commande.','options':[{'label':'Contrôler les commandes et réapprovisionner','tradeoff':'Réduit le risque de rupture, mais mobilise de la trésorerie.'},{'label':'Réviser les quantités proposées à la vente','tradeoff':'Évite une promesse de livraison impossible.'}],'action':'Ouvrir les stocks','module':'products','priority':3 if r['stock']==0 else 2})
        bank=c.execute('SELECT COALESCE(SUM(amount_cents),0) FROM bank_transactions').fetchone()[0]
        if bank<0:
            out.append({'key':'cash-negative','severity':'critical','category':'Trésorerie','title':'Solde des mouvements bancaires négatif','detail':f'Somme des mouvements enregistrés : {bank/100:.2f} €. Ce total n’est pas un solde bancaire certifié sans solde initial et rapprochement.','impact':'Possible tension de trésorerie à confirmer avec les relevés bancaires.','options':[{'label':'Vérifier le solde bancaire réel et les opérations manquantes','tradeoff':'Fiabilise le diagnostic avant toute décision.'},{'label':'Prioriser les encaissements et étudier les dépenses reportables','tradeoff':'Améliore potentiellement la trésorerie, mais certains paiements sont obligatoires.'}],'action':'Ouvrir la banque','module':'finance','priority':4})
    out.sort(key=lambda x:(-x['priority'],x['key']))
    return {'count':len(out),'critical':sum(x['severity']=='critical' for x in out),'high':sum(x['severity']=='high' for x in out),'alerts':out,'rules':['Facture validée non encaissée depuis 30 jours (date de création, pas date d’échéance)','Stock inférieur ou égal à 5 unités (seuil de démonstration)','Somme des mouvements bancaires négative (sans solde initial)']}


# --- v0.5 : cockpit décisionnel, simulation et traçabilité ---
from fastapi.responses import Response
from datetime import date
from datetime import datetime, timedelta
import csv

def init_v5():
    with connect() as c:
        c.executescript("""CREATE TABLE IF NOT EXISTS decisions(
            id INTEGER PRIMARY KEY,alert_key TEXT NOT NULL,title TEXT NOT NULL,
            chosen_option TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',
            created TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_decisions_alert ON decisions(alert_key);
        """)
init_v5()

class DecisionInput(BaseModel):
    alert_key:str=Field(min_length=1,max_length=100)
    title:str=Field(min_length=1,max_length=250)
    chosen_option:str=Field(min_length=1,max_length=500)
    note:str=Field(default='',max_length=2000)

@app.post('/api/decisions')
def create_decision(x:DecisionInput):
    active={a['key']:a for a in alerts()['alerts']}
    alert=active.get(x.alert_key)
    if not alert:raise HTTPException(400,'Alerte non active ou inconnue')
    if x.chosen_option not in [o['label'] for o in alert['options']]:
        raise HTTPException(400,'Option non proposée pour cette alerte')
    with connect() as c:
        cur=c.execute('INSERT INTO decisions(alert_key,title,chosen_option,note,created) VALUES(?,?,?,?,?)',
            (x.alert_key,alert['title'],x.chosen_option,x.note,datetime.now().isoformat(timespec='seconds')))
        return {'id':cur.lastrowid,'status':'recorded','message':'Décision consignée. Aucune action externe exécutée.'}

@app.get('/api/decisions')
def get_decisions():
    return records('SELECT * FROM decisions ORDER BY id DESC')

class Scenario(BaseModel):
    expected_receipts:Decimal=Field(default=0,ge=0,le=100000000)
    expected_payments:Decimal=Field(default=0,ge=0,le=100000000)
    collection_rate:int=Field(default=100,ge=0,le=100)

@app.post('/api/scenario')
def scenario(x:Scenario):
    f=finance();d=dashboard()
    start=f['bank_balance_cents']
    unpaid=d['unpaid_cents']
    collected=(unpaid*x.collection_rate+50)//100
    inflow=cents(x.expected_receipts)
    outflow=cents(x.expected_payments)
    ending=start+collected+inflow-outflow
    return {'start_cents':start,'unpaid_cents':unpaid,'collection_rate':x.collection_rate,
            'collected_cents':collected,'additional_receipts_cents':inflow,
            'additional_payments_cents':outflow,'ending_cents':ending,
            'risk':'critical' if ending<0 else 'medium' if ending<100000 else 'low',
            'formula':'Mouvements bancaires cumulés + créances encaissées simulées + autres encaissements - décaissements',
            'limitations':'Simulation indicative, sans solde bancaire initial certifié, dates d’échéance ni flux réels. Les créances simulées ne sont pas automatiquement enregistrées.'}

@app.get('/api/export/entries.csv')
def export_entries():
    rows=records('SELECT i.number,e.account,e.debit_cents,e.credit_cents FROM entries e JOIN invoices i ON i.id=e.invoice_id ORDER BY e.id')
    buf=__import__('io').StringIO();w=csv.writer(buf,delimiter=';')
    w.writerow(['Facture','Compte','Débit EUR','Crédit EUR'])
    for r in rows:w.writerow([r['number'],r['account'],f"{r['debit_cents']/100:.2f}",f"{r['credit_cents']/100:.2f}"])
    return Response(content='\ufeff'+buf.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="nova_journal_demo.csv"'})

@app.post('/api/demo/load')
def demo_load():
    with connect() as c:
        total=sum(c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ['clients','products','invoices','suppliers'])
        if total:raise HTTPException(409,'Données existantes : chargement démo réservé à une base vide.')
        client=c.execute('INSERT INTO clients(name,email) VALUES(?,?)',('Atelier Horizon','contact@exemple.test')).lastrowid
        c.execute('INSERT INTO clients(name,email) VALUES(?,?)',('Studio Azur','contact2@exemple.test'))
        c.execute('INSERT INTO suppliers(name,email) VALUES(?,?)',('Fournitures Démo','achats@exemple.test'))
        c.execute('INSERT INTO products(name,price_cents,stock) VALUES(?,?,?)',('Bureau design',45000,2))
        c.execute('INSERT INTO products(name,price_cents,stock) VALUES(?,?,?)',('Chaise ergonomique',18000,0))
        c.execute('INSERT INTO products(name,price_cents,stock) VALUES(?,?,?)',('Lampe de bureau',6500,18))
        old=(date.today()-timedelta(days=43)).isoformat()
        cur=c.execute('INSERT INTO invoices(number,client_id,created,status,subtotal_cents,vat_cents,total_cents) VALUES(?,?,?,?,?,?,?)',
                      (f'FAC-{date.today().year}-00001',client,old,'validated',45000,9000,54000))
        c.execute('INSERT INTO invoice_lines(invoice_id,product_id,description,qty,unit_cents,vat_rate) VALUES(?,?,?,?,?,?)',(cur.lastrowid,1,'Bureau design',1,45000,20))
        for acc,debit,credit in [('411',54000,0),('707',0,45000),('44571',0,9000)]:
            c.execute('INSERT INTO entries(invoice_id,account,debit_cents,credit_cents) VALUES(?,?,?,?)',(cur.lastrowid,acc,debit,credit))
        c.execute('INSERT INTO bank_transactions(label,amount_cents,created) VALUES(?,?,?)',('Charges de démonstration',-85000,date.today().isoformat()))
        c.execute('INSERT INTO bank_transactions(label,amount_cents,created) VALUES(?,?,?)',('Encaissement de démonstration',30000,date.today().isoformat()))
    return {'status':'loaded','note':'Données fictives créées pour la soutenance'}


# TVA - simulation pédagogique de CA3, sans télédéclaration fiscale.
class PurchaseVat(BaseModel):
    supplier_id:int
    label:str=Field(min_length=1,max_length=250)
    invoice_date:date
    amount_ht:Decimal=Field(ge=0)
    vat_rate:Decimal=Field(default=Decimal('20'),ge=0,le=100)
    deductible:bool=True

def init_vat():
    with connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS purchase_vat (
          id INTEGER PRIMARY KEY, supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
          label TEXT NOT NULL, invoice_date TEXT NOT NULL,
          ht_cents INTEGER NOT NULL, vat_cents INTEGER NOT NULL,
          deductible INTEGER NOT NULL DEFAULT 1)""")
init_vat()

@app.get('/api/vat/purchases')
def vat_purchases():
    return records("SELECT p.*,s.name supplier FROM purchase_vat p JOIN suppliers s ON s.id=p.supplier_id ORDER BY p.id DESC")

@app.post('/api/vat/purchases')
def vat_add_purchase(x:PurchaseVat):
    with connect() as c:
        if not c.execute('SELECT 1 FROM suppliers WHERE id=?',(x.supplier_id,)).fetchone():
            raise HTTPException(400,'Fournisseur introuvable')
        ht=cents(x.amount_ht)
        vat=int((Decimal(ht)*x.vat_rate/Decimal(100)).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
        cur=c.execute('INSERT INTO purchase_vat(supplier_id,label,invoice_date,ht_cents,vat_cents,deductible) VALUES(?,?,?,?,?,?)',
                      (x.supplier_id,x.label,x.invoice_date.isoformat(),ht,vat,int(x.deductible)))
        return {'id':cur.lastrowid,'ht_cents':ht,'vat_cents':vat}

@app.get('/api/vat/summary')
def vat_summary(year:int,month:int=0,quarter:int=0,previous_credit_cents:int=0):
    if year<2000 or year>2100 or month not in range(13) or quarter not in range(5) or (month and quarter) or previous_credit_cents<0:
        raise HTTPException(400,'Période ou crédit antérieur invalide')
    if month: start=f'{year}-{month:02d}-01'; end=f'{year+1}-01-01' if month==12 else f'{year}-{month+1:02d}-01'
    elif quarter:
        m=1+(quarter-1)*3; start=f'{year}-{m:02d}-01';end=f'{year+1}-01-01' if quarter==4 else f'{year}-{m+3:02d}-01'
    else: start=f'{year}-01-01';end=f'{year+1}-01-01'
    with connect() as c:
        # Only validated/paid sales invoices, not drafts.
        sale=c.execute("SELECT COALESCE(SUM(subtotal_cents),0),COALESCE(SUM(vat_cents),0) FROM invoices WHERE created>=? AND created<? AND status IN ('validated','paid')",(start,end)).fetchone()
        pur=c.execute('SELECT COALESCE(SUM(ht_cents),0),COALESCE(SUM(CASE WHEN deductible=1 THEN vat_cents ELSE 0 END),0),COALESCE(SUM(CASE WHEN deductible=0 THEN vat_cents ELSE 0 END),0) FROM purchase_vat WHERE invoice_date>=? AND invoice_date<?',(start,end)).fetchone()
    net=sale[1]-pur[1]-previous_credit_cents
    return {'start':start,'end_exclusive':end,'sales_ht_cents':sale[0],'vat_collected_cents':sale[1],
            'purchases_ht_cents':pur[0],'vat_deductible_cents':pur[1], 'vat_non_deductible_cents':pur[2],
            'previous_credit_cents':previous_credit_cents,'vat_payable_cents':max(net,0),
            'vat_credit_cents':max(-net,0), 'net_cents':net,
            'warning':'Simulation pédagogique simplifiée : ne tient pas compte des règles d’exigibilité, opérations intracommunautaires, autoliquidation, imports, régularisations et autres rubriques CA3. Ne pas utiliser pour une déclaration réelle.'}

@app.get('/api/health')
def health(): return {'status':'ok'}
