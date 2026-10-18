"""Standalone repro (raw Cypher, no Contraflow code): algo.maxFlow returns 2550,
true maximum flow is 2700 (n15->n33->n75: 2400; n15->n33->n7->n69->n75: 150;
n15->n40->n30->n53->n25->n5->n73->n69->n75: 150).
Usage: python falkordb_maxflow_bug_repro.py [port]"""
import sys
from falkordb import FalkorDB
EDGES = [("n15","n33",3600),("n7","n69",150),("n25","n7",150),("n5","n73",1200),("n25","n5",1200),
         ("n53","n25",450),("n15","n40",150),("n33","n7",300),("n40","n30",300),("n73","n69",600),
         ("n69","n75",600),("n33","n75",2400),("n30","n53",150)]
port = int(sys.argv[1]) if len(sys.argv) > 1 else 6379
db = FalkorDB(port=port); g = db.select_graph("maxflow_repro")
try: g.delete()
except Exception: pass
for u, v, c in EDGES:
    g.query("MERGE (a:N {name:$u}) MERGE (b:N {name:$v}) CREATE (a)-[:R {cap:$c}]->(b)", {"u": u, "v": v, "c": c})
r = g.query("MATCH (s:N {name:'n15'}), (t:N {name:'n75'}) CALL algo.maxFlow({sourceNodes:[s], targetNodes:[t], "
            "capacityProperty:'cap', relationshipTypes:['R']}) YIELD maxFlow RETURN maxFlow").result_set[0][0]
ver = db.connection.execute_command("MODULE", "LIST")[0][3]
print(f"module ver {ver}: algo.maxFlow = {r}, expected 2700 -> {'OK' if r == 2700 else 'WRONG'}")
g.delete()
