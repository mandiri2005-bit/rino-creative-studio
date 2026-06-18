-- Representative LAB seed. Run on SOURCE as superuser (RLS bypassed). Fixed UUIDs = deterministic.
-- Real mode needs no seed — real data arrives via logical replication from live Neon.
INSERT INTO tenants (id,name,slug,email) VALUES
 ('11111111-1111-1111-1111-111111111111','Tenant A','tenant-a','a@example.com'),
 ('22222222-2222-2222-2222-222222222222','Tenant B','tenant-b','b@example.com')
ON CONFLICT DO NOTHING;
INSERT INTO users (id,tenant_id,email,role) VALUES
 ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','11111111-1111-1111-1111-111111111111','u1@a.com','member'),
 ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb','22222222-2222-2222-2222-222222222222','u2@b.com','member')
ON CONFLICT DO NOTHING;
INSERT INTO credit_ledger (tenant_id,user_id,delta,reason) VALUES
 ('11111111-1111-1111-1111-111111111111','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',1000,'topup'),
 ('11111111-1111-1111-1111-111111111111','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', -50,'charge'),
 ('22222222-2222-2222-2222-222222222222','bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 500,'topup'),
 ('22222222-2222-2222-2222-222222222222','bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', -20,'charge');
INSERT INTO credit_balances (tenant_id,balance) VALUES
 ('11111111-1111-1111-1111-111111111111',950),
 ('22222222-2222-2222-2222-222222222222',480)
ON CONFLICT (tenant_id) DO UPDATE SET balance=EXCLUDED.balance;
