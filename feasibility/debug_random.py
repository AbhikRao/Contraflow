import random, sys
from falkordb import FalkorDB
sys.path.insert(0,'.')
from tests.test_maxflow_correctness import random_graph, run_case
from feasibility.flowlib import Edge
port=int(sys.argv[1])
rng = random.Random(20261015)
for k in range(60):
    nodes, edges = random_graph(rng)
    src = rng.sample(nodes[:10], rng.choice([1, 2, 3]))
    dst = rng.sample(nodes[-10:], rng.choice([1, 2, 3]))
    mode = k % 3
    if mode == 0: edges = [e for e in edges if rng.random() > 0.15]
    elif mode == 1: edges = [Edge(e.eid, e.u, e.v, 0.0 if rng.random() < 0.15 else e.cap) for e in edges]
    else: edges = [Edge(e.eid, e.u, e.v, e.cap * rng.choice([0.5, 1.0, 1.0, 1.5, 2.0])) for e in edges]
    try:
        checks, value, ref, cut = run_case(port, f"rand{k}", edges, src, dst)
    except Exception as ex:
        print(k, 'mode', mode, 'EXC', ex, 'src',src,'dst',dst); continue
    bad = {a:b for a,b in checks.items() if b is False}
    if bad: print(k,'mode',mode,'val',value,'nx',ref,'cut',cut['cut_capacity'],'worst',checks['worst_conservation_error'],bad, 'src',src,'dst',dst)
