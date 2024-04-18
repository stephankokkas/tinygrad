#!/usr/bin/env python3
import os
os.environ["NOOPT"] = "1"
from tinygrad import Device, nn, Tensor, dtypes
#Device.DEFAULT = "CLANG"
from train_gpt2 import GPT, GPTConfig
from tinygrad.helpers import dedup, to_function_name, flatten
from tinygrad.engine.schedule import create_schedule
from tinygrad.engine.realize import memory_planner, run_schedule
from tinygrad.ops import BufferOps, LoadOps
from tinygrad.runtime.ops_clang import CLANG_PROGRAM_HEADER

if __name__ == "__main__":
  model = GPT(GPTConfig(n_layer=2, n_head=12, n_embd=768))
  #model.load_pretrained()
  seen = set()
  early_sched = create_schedule([x.lazydata for x in nn.state.get_parameters(model)], seen)
  print(f"built model {len(early_sched)}")

  optimizer = nn.optim.Adam(nn.state.get_parameters(model), lr=1e-4)
  for i in range(3):  # TODO: why does it take three and not two to stablize
    X = Tensor.empty(4, 64, dtype=dtypes.int)
    Y = Tensor.empty(4, 64, dtype=dtypes.int)
    _, loss = model(X, Y)
    optimizer.zero_grad()
    loss.backward()
    tensors = optimizer.schedule_step()
    sched = create_schedule([loss.lazydata] + [x.lazydata for x in tensors], seen)
    print(f"calls {i}:", len(sched))
    #run_schedule(sched[:])
  del seen  # free the LazyBuffers
  sched = memory_planner(sched)
  ast_dedup = dedup([si.ast for si in sched if si.ast[0].op is BufferOps.STORE])
  srcs = {}
  for ast in ast_dedup:
    k = Device["CLANG"].get_linearizer(*ast)
    k.linearize()
    src = Device["CLANG"].compiler.render(to_function_name(k.name), k.uops).strip(CLANG_PROGRAM_HEADER)
    srcs[ast] = (k.name, src)
  print("functions:", len(srcs))
  all_buffers = dedup(flatten([si.outputs+si.inputs for si in sched]))
  numbered_bufs = {x:i for i,x in enumerate(all_buffers)}
  print("buffers:", len(numbered_bufs))

  state_dict = nn.state.get_state_dict(model)
  state_dict.update({'X': X, 'Y': Y, 'loss': loss})
  state_dict.update({'adam_b1': optimizer.b1, 'adam_b2': optimizer.b2, 'adam_t': optimizer.t})
  inverse_state_dict = {v:k for k,v in state_dict.items()}
  for p,m,v in zip(optimizer.params, optimizer.m, optimizer.v):
    nm = inverse_state_dict[p]
    state_dict["adam_m_"+nm] = m
    state_dict["adam_v_"+nm] = v
  named_buffers = {v.lazydata.base.buffer:k.replace(".", "_") for k,v in state_dict.items()}

  for si in sched:
    bufs = [named_buffers.get(b, f"b{numbered_bufs[b]}") for b in si.outputs+si.inputs]
    if si.ast[0].op is not BufferOps.STORE:
      print(f"// {si.ast[0].op}", bufs)
    else:
      print(f"{srcs[si.ast][0]}({', '.join(bufs)})")
      #print(srcs[si.ast][1])