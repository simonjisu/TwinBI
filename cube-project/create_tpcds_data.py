import duckdb
from loguru import logger
from pathlib import Path

def create_db(db_path: str):
    con = duckdb.connect(database=db_path)
    con.execute('INSTALL tpcds;')
    con.execute('LOAD tpcds;')
    logger.info("Generating TPC-DS data...")
    con.execute("CALL dsdgen(sf = 1);")  # run only once generate data with scale factor 1 (1GB)
    logger.info(f"TPC-DS data created in {db_path}")

if __name__ == "__main__":

    execution_path = Path().resolve()
    if execution_path.name.lower() != "cube-project":
        assert False, "This script must be run from the cube-project directory"

    if not (execution_path / "data").exists():
        (execution_path / "data").mkdir(parents=True)

    create_db(db_path=str(execution_path / "data" / "tpcds.db"))