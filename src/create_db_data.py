import duckdb
from loguru import logger
from pathlib import Path
import argparse
import duckdb
import random
from faker import Faker
from datetime import datetime, timedelta
import argparse
from pathlib import Path
from loguru import logger

def create_tables(con):
    # drop existing tables if any
    con.execute("DROP TABLE IF EXISTS fact_sales;")
    con.execute("DROP TABLE IF EXISTS dim_product;")
    con.execute("DROP TABLE IF EXISTS dim_store;")
    con.execute("DROP TABLE IF EXISTS dim_date;")
    
    con.execute("""
    CREATE TABLE IF NOT EXISTS dim_product (
        product_key INTEGER PRIMARY KEY,
        product_name VARCHAR,
        brand VARCHAR,
        type VARCHAR,
        category VARCHAR,
        department VARCHAR,
        marketing_group VARCHAR
    );
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS dim_store (
        store_key INTEGER PRIMARY KEY,
        store_name VARCHAR,
        sales_manager VARCHAR,
        sales_district VARCHAR,
        city VARCHAR,
        state VARCHAR
    );
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS dim_date (
        date_key INTEGER PRIMARY KEY,
        date DATE,
        week INTEGER,
        month INTEGER,
        quarter INTEGER,
        year INTEGER
    );
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS fact_sales (
        sale_id INTEGER PRIMARY KEY,
        product_key INTEGER REFERENCES dim_product(product_key),
        store_key INTEGER REFERENCES dim_store(store_key),
        date_key INTEGER REFERENCES dim_date(date_key),
        units_sold INTEGER,
        unit_price DECIMAL(10,2),
        total_receipts DECIMAL(12,2)
    );
    """)

def populate_dimensions(con, fake):
    products, brands, types, categories, departments, marketing_groups = [], ["Nike","Apple","Sony","Samsung","LG","Adidas"], ["Electronics","Clothing","Appliance","Accessory"], ["Mobile","TV","Shoes","Laptop","Audio"], ["Marketing","Sales","Tech"], ["A","B","C"]
    for i in range(1, 51):
        products.append((i, fake.word().capitalize(), random.choice(brands), random.choice(types), random.choice(categories), random.choice(departments), random.choice(marketing_groups)))
    con.executemany("INSERT INTO dim_product VALUES (?, ?, ?, ?, ?, ?, ?)", products)

    stores, states, cities, sales_districts = [], ["California","Texas","New York","Florida"], ["Los Angeles","Houston","Dallas","Miami","New York City"], ["West","South","East","North"]
    for i in range(1, 21):
        stores.append((
            i,
            f"Store_{i}",
            fake.name(),
            random.choice(sales_districts),
            random.choice(cities),
            random.choice(states)
        ))
    con.executemany("INSERT INTO dim_store VALUES (?, ?, ?, ?, ?, ?)", stores)

    dates, start_date = [], datetime(2022, 1, 1)
    for i in range(1, 3 * 365 + 1):
        d = start_date + timedelta(days=i-1)
        dates.append((int(d.strftime("%Y%m%d")), d.date(), d.isocalendar()[1], d.month, (d.month-1)//3 + 1, d.year))
    con.executemany("INSERT INTO dim_date VALUES (?, ?, ?, ?, ?, ?)", dates)
    return dates

def populate_facts(con, dates, num_rows):
    facts = []
    for i in range(1, num_rows + 1):
        product_key = random.randint(1, 50)
        store_key = random.randint(1, 20)
        date_key = random.choice(dates)[0]
        units_sold = random.randint(1, 20)
        unit_price = round(random.uniform(10, 2000), 2)
        total_receipts = round(units_sold * unit_price, 2)
        facts.append((i, product_key, store_key, date_key, units_sold, unit_price, total_receipts))
    con.executemany("INSERT INTO fact_sales VALUES (?, ?, ?, ?, ?, ?, ?)", facts)

def create_sales_db(args):
    fake = Faker()
    Faker.seed(42)
    random.seed(42)
    con = duckdb.connect(args.db_path)
    create_tables(con)
    dates = populate_dimensions(con, fake)
    populate_facts(con, dates, args.rows)
    logger.info(f"Data generated successfully in {args.db_path}")

def create_tpcds_db(db_path: str):
    con = duckdb.connect(database=db_path)
    con.execute('INSTALL tpcds;')
    con.execute('LOAD tpcds;')
    logger.info("Generating TPC-DS data...")
    con.execute("CALL dsdgen(sf = 1);")  # run only once generate data with scale factor 1 (1GB)
    logger.info(f"TPC-DS data created in {db_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create TPC-DS data in a DuckDB database.")
    parser.add_argument(
        "--type",
        type=str,
        default="tpcds",
        help="Type of the dataset to create. Currently only 'tpcds' is supported.",
    )
    parser.add_argument(
        "--db_path",
        type=str,
        default="data/tpcds.db",
        help="Path to the DuckDB database file where TPC-DS data will be created.",
    )
    parser.add_argument("--rows", type=int, default=2000, help="Number of fact table rows to generate")
    args = parser.parse_args()

    if args.type.lower() == "tpcds":
        create_tpcds_db(db_path=args.db_path)
    elif args.type.lower() == "sales":
        create_sales_db(args)
    else:
        logger.error(f"Unsupported dataset type: {args.type}")


    # uv run src/create_db_data.py --type sales --rows 5000 --db_path "./data/tutorial/database/sales.db"
