"""Build the sample shop.db — a tiny, deterministic synthetic retail database.

No randomness, no downloads: every row is spelled out here so the DB is reproducible and reviewable.
Run `python build_db.py` to (re)generate `shop.db` next to this file. The data is entirely fictional
and domain-neutral (products/customers/orders across four regions).

Schema:
  regions(id, name)
  products(id, name, category, price)
  customers(id, name, region_id)
  orders(id, customer_id, product_id, quantity, order_date)   -- dates are fixed 2024 values
"""
import os
import sqlite3

REGIONS = [
    (1, "East"), (2, "West"), (3, "North"), (4, "South"),
]

PRODUCTS = [
    # id, name, category, price
    (1, "Aurora Laptop", "Electronics", 1200.0),
    (2, "Nimbus Phone", "Electronics", 800.0),
    (3, "Pulse Earbuds", "Electronics", 150.0),
    (4, "Vertex Monitor", "Electronics", 320.0),
    (5, "Cedar Desk", "Furniture", 450.0),
    (6, "Willow Chair", "Furniture", 180.0),
    (7, "Basalt Shelf", "Furniture", 90.0),
    (8, "Solar Lamp", "Furniture", 60.0),
    (9, "Field Notebook", "Stationery", 12.0),
    (10, "Ink Pen Set", "Stationery", 25.0),
    (11, "Canvas Tote", "Accessories", 40.0),
    (12, "Quartz Watch", "Accessories", 240.0),
]

CUSTOMERS = [
    # id, name, region_id
    (1, "Alice Tan", 1),
    (2, "Bruno Sato", 1),
    (3, "Carmen Diaz", 2),
    (4, "Dmitri Volkov", 2),
    (5, "Emi Nakamura", 3),
    (6, "Farid Hassan", 3),
    (7, "Grace Lee", 4),
    (8, "Hugo Martin", 4),
    (9, "Ivy Chen", 1),
    (10, "Jonas Berg", 2),
]

ORDERS = [
    # id, customer_id, product_id, quantity, order_date
    # Designed for harder analytics: multi-month customers, ties, per-region/per-category structure,
    # one customer (Hugo, id 8) who never buys Electronics (anti-join), products sold across regions.
    (1, 1, 1, 1, "2024-01-15"),
    (2, 1, 3, 2, "2024-01-20"),
    (3, 2, 2, 1, "2024-02-03"),
    (4, 3, 5, 1, "2024-02-11"),
    (5, 3, 6, 4, "2024-02-14"),
    (6, 4, 4, 2, "2024-03-02"),
    (7, 5, 1, 1, "2024-03-05"),
    (8, 5, 9, 10, "2024-03-09"),
    (9, 6, 12, 1, "2024-03-15"),
    (10, 7, 2, 2, "2024-03-22"),
    (11, 8, 6, 3, "2024-04-01"),
    (12, 1, 4, 1, "2024-04-04"),
    (13, 2, 6, 2, "2024-04-10"),
    (14, 9, 1, 1, "2024-04-18"),
    (15, 10, 5, 1, "2024-05-02"),
    (16, 3, 2, 1, "2024-05-07"),
    (17, 4, 11, 5, "2024-05-15"),
    (18, 5, 12, 2, "2024-05-21"),
    (19, 6, 3, 1, "2024-06-03"),
    (20, 7, 7, 3, "2024-06-09"),
    (21, 8, 8, 4, "2024-06-14"),
    (22, 9, 2, 1, "2024-06-20"),
    (23, 10, 4, 1, "2024-06-28"),
    (24, 1, 10, 2, "2024-07-02"),
    (25, 2, 1, 1, "2024-07-11"),
    # extra months + volume so window/date/HAVING questions have real answers
    (26, 1, 2, 1, "2024-05-19"),
    (27, 1, 6, 1, "2024-06-25"),
    (28, 2, 4, 1, "2024-06-06"),
    (29, 2, 9, 6, "2024-07-18"),
    (30, 3, 1, 3, "2024-06-30"),
    (31, 3, 12, 1, "2024-07-24"),
    (32, 4, 2, 1, "2024-07-05"),
    (33, 4, 6, 2, "2024-08-02"),
    (34, 5, 5, 1, "2024-06-11"),
    (35, 5, 3, 2, "2024-07-27"),
    (36, 6, 1, 1, "2024-05-28"),
    (37, 6, 7, 2, "2024-07-14"),
    (38, 7, 12, 1, "2024-04-22"),
    (39, 7, 1, 1, "2024-08-05"),
    (40, 8, 7, 2, "2024-05-30"),
    (41, 8, 10, 3, "2024-07-09"),
    (42, 9, 4, 1, "2024-05-24"),
    (43, 9, 6, 3, "2024-08-08"),
    (44, 10, 2, 1, "2024-06-16"),
    (45, 10, 11, 2, "2024-08-12"),
    (46, 1, 1, 1, "2024-08-15"),
    (47, 3, 5, 2, "2024-08-20"),
    (48, 5, 1, 1, "2024-08-25"),
]


def build(db_path):
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("CREATE TABLE regions (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    c.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
              "category TEXT NOT NULL, price REAL NOT NULL)")
    c.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
              "region_id INTEGER NOT NULL REFERENCES regions(id))")
    c.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL "
              "REFERENCES customers(id), product_id INTEGER NOT NULL REFERENCES products(id), "
              "quantity INTEGER NOT NULL, order_date TEXT NOT NULL)")
    c.executemany("INSERT INTO regions VALUES (?,?)", REGIONS)
    c.executemany("INSERT INTO products VALUES (?,?,?,?)", PRODUCTS)
    c.executemany("INSERT INTO customers VALUES (?,?,?)", CUSTOMERS)
    c.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", ORDERS)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shop.db")
    build(path)
    print(f"built {path}")
