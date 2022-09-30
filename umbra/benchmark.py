import csv
import datetime
from dateutil.relativedelta import relativedelta
import os
import re
import psycopg2
import time
import sys
from queries import run_script, run_queries, run_precomputations
from pathlib import Path
from itertools import cycle
import argparse


def execute(cur, query):
    #start = time.time()
    cur.execute(query)
    #end = time.time()
    #if end - start >= 0.100:
    #    print(f"Duration: {end - start}:\n{query}")


query_variants = ["1", "2a", "2b", "3", "4", "5", "6", "7", "8a", "8b", "9", "10a", "10b", "11", "12", "13", "14a", "14b", "15a", "15b", "16a", "16b", "17", "18", "19a", "19b", "20a", "20b"]

def run_batch_updates(pg_con, data_dir, batch_start_date, timings_file):
    # format date to yyyy-mm-dd
    batch_id = batch_start_date.strftime('%Y-%m-%d')
    batch_dir = f"batch_id={batch_id}"
    print(f"#################### {batch_dir} ####################")

    start = time.time()

    print("## Inserts")
    for entity in insert_entities:
        batch_path = f"{data_dir}/inserts/dynamic/{entity}/{batch_dir}"
        if not os.path.exists(batch_path):
            continue

        for csv_file in [f for f in os.listdir(batch_path) if f.endswith(".csv")]:
            csv_path = f"{batch_path}/{csv_file}"
            print(f"- {csv_path}")
            execute(cur, f"COPY {entity} FROM '{dbs_data_dir}/inserts/dynamic/{entity}/{batch_dir}/{csv_file}' (DELIMITER '|', HEADER, NULL '', FORMAT text)")
            if entity == "Person_knows_Person":
                execute(cur, f"COPY {entity} (creationDate, Person2id, Person1id) FROM '{dbs_data_dir}/inserts/dynamic/{entity}/{batch_dir}/{csv_file}' (DELIMITER '|', HEADER, NULL '', FORMAT text)")
            pg_con.commit()

    print("## Deletes")
    # Deletes are implemented using a SQL script which use auxiliary tables.
    # Entities to be deleted are first put into {entity}_Delete_candidate tables.
    # These are cleaned up before running the delete script.
    for entity in delete_entities:
        execute(cur, f"DELETE FROM {entity}_Delete_candidates")

        batch_path = f"{data_dir}/deletes/dynamic/{entity}/{batch_dir}"
        if not os.path.exists(batch_path):
            continue

        for csv_file in [f for f in os.listdir(batch_path) if f.endswith(".csv")]:
            csv_path = f"{batch_path}/{csv_file}"
            print(f"- {csv_path}")
            execute(cur, f"COPY {entity}_Delete_candidates FROM '{dbs_data_dir}/deletes/dynamic/{entity}/{batch_dir}/{csv_file}' (DELIMITER '|', HEADER, NULL '', FORMAT text)")
            pg_con.commit()

    print("Maintain materialized views . . .")
    run_script(pg_con, cur, "dml/maintain-views.sql")
    print("Done.")
    print()

    print("Apply deletes . . .")
    # Invoke delete script which makes use of the {entity}_Delete_candidates tables
    run_script(pg_con, cur, "dml/apply-deletes.sql")
    print("Done.")
    print()

    print("Apply precomp . . .")
    run_precomputations(query_variants, pg_con, cur, batch_id, sf, timings_file)
    print("Done.")
    print()

    end = time.time()
    duration = end - start
    timings_file.write(f"Umbra|{sf}|{batch_id}|writes||{duration}\n")


parser = argparse.ArgumentParser()
parser.add_argument('--scale_factor', type=float, help='Scale factor', required=True)
parser.add_argument('--test', action='store_true', help='Test execution: 1 query/batch', required=False)
parser.add_argument('--pgtuning', action='store_true', help='Paramgen tuning execution: 100 queries/batch', required=False)
parser.add_argument('--local', action='store_true', help='Local run (outside of a container)', required=False)
parser.add_argument('--data_dir', type=str, help='Directory with the initial_snapshot, insert, and delete directories', required=True)
args = parser.parse_args()
sf = args.scale_factor
test = args.test
pgtuning = args.pgtuning
local = args.local
data_dir = args.data_dir

if local:
    dbs_data_dir = data_dir
else:
    dbs_data_dir = '/data'

parameter_csvs = {}
for query_variant in query_variants:
    # wrap parameters into infinite loop iterator
    parameter_csvs[query_variant] = cycle(csv.DictReader(open(f'../parameters/parameters-sf{sf}/bi-{query_variant}.csv'), delimiter='|'))

print(f"- Input data directory, ${{UMBRA_CSV_DIR}}: {data_dir}")

insert_nodes = ["Comment", "Forum", "Person", "Post"]
insert_edges = ["Comment_hasTag_Tag", "Forum_hasMember_Person", "Forum_hasTag_Tag", "Person_hasInterest_Tag", "Person_knows_Person", "Person_likes_Comment", "Person_likes_Post", "Person_studyAt_University", "Person_workAt_Company",  "Post_hasTag_Tag"]
insert_entities = insert_nodes + insert_edges

# set the order of deletions to reflect the dependencies between node labels (:Comment)-[:REPLY_OF]->(:Post)<-[:CONTAINER_OF]-(:Forum)-[:HAS_MODERATOR]->(:Person)
delete_nodes = ["Comment", "Post", "Forum", "Person"]
delete_edges = ["Forum_hasMember_Person", "Person_knows_Person", "Person_likes_Comment", "Person_likes_Post"]
delete_entities = delete_nodes + delete_edges

output = Path(f'output/output-sf{sf}')
output.mkdir(parents=True, exist_ok=True)
open(f"output/output-sf{sf}/results.csv", "w").close()
open(f"output/output-sf{sf}/timings.csv", "w").close()

timings_file = open(f"output/output-sf{sf}/timings.csv", "a")
timings_file.write(f"tool|sf|day|q|parameters|time\n")
results_file = open(f"output/output-sf{sf}/results.csv", "a")

pg_con = psycopg2.connect(host="localhost", user="postgres", password="mysecretpassword", port=8000)
pg_con.autocommit = True
cur = pg_con.cursor()

run_script(pg_con, cur, f"ddl/schema-delete-candidates.sql");


network_start_date = datetime.date(2012, 11, 29)
network_end_date = datetime.date(2013, 1, 1)
test_end_date = datetime.date(2012, 12, 2)
batch_size = relativedelta(days=1)
batch_date = network_start_date

if pgtuning:
    run_queries(query_variants, parameter_csvs, pg_con, sf, test, pgtuning, batch_date, timings_file, results_file)
else:
    # Run alternating write-read blocks.
    # The first write-read block is the power batch, while the rest are the throughput batches.
    while batch_date < network_end_date and (not test or batch_date < test_end_date):
        run_batch_updates(pg_con, data_dir, batch_date, timings_file)
        reads_time = run_queries(query_variants, parameter_csvs, pg_con, sf, test, pgtuning, batch_date, timings_file, results_file)
        timings_file.write(f"Umbra|{sf}|{batch_date}|reads||{reads_time:.6f}\n")

        batch_date = batch_date + batch_size

timings_file.close()
results_file.close()

cur.close()
pg_con.close()
