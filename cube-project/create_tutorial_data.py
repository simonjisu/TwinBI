import duckdb
import random
from faker import Faker
from datetime import datetime, timedelta
import argparse
from pathlib import Path
from loguru import logger

def create_tables(con):
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

    dates, start_date = [], datetime(2024, 1, 1)
    for i in range(1, 366):
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

def main(args):
    fake = Faker()
    Faker.seed(42)
    random.seed(42)
    con = duckdb.connect(args.output)
    create_tables(con)
    dates = populate_dimensions(con, fake)
    populate_facts(con, dates, args.rows)
    logger.info(f"Data generated successfully in {args.output}")

if __name__ == "__main__":
    
    execution_path = Path().resolve()
    if execution_path.name.lower() != "cube-project":
        assert False, "This script must be run from the cube-project directory"

    parser = argparse.ArgumentParser(description="Generate synthetic sales data in DuckDB using Faker.")
    parser.add_argument("--rows", type=int, default=2000, help="Number of fact table rows to generate")
    parser.add_argument("--output", type=str, default="sales_star_schema.duckdb", help="Output DuckDB file name")
    args = parser.parse_args()
    main(args)

    # uv run create_tutorial_data.py --rows 2000 --output "./data/tutorial/tutorial.db"
