from main import db, init_db

init_db()
with db(write=True) as conn:
    conn.execute("INSERT OR IGNORE INTO tenants (id,name,plan_id) VALUES ('demo-free','Demo Free','free')")
    conn.execute("INSERT OR IGNORE INTO tenants (id,name,plan_id) VALUES ('demo-pro','Demo Pro','pro')")
print("Seeded demo-free and demo-pro")
