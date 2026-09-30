#!/usr/bin/env python3
"""CYTHANX SOMATAFORM + XMRIG DISPATCHER"""
from __future__ import annotations
import hashlib, json, os, signal, subprocess, time
from dataclasses import dataclass, asdict

XMRIG_PATH=os.environ.get("XMRIG_PATH","./xmrig")
XMRIG_CONFIG=os.environ.get("XMRIG_CONFIG","config.json")
CYCLES=int(os.environ.get("CYTHANX_CYCLES","7"))
INTERVAL=float(os.environ.get("CYTHANX_INTERVAL","1.0"))
RESTART_DELAY=3.0

@dataclass
class State:
    cycle:int=0; signal:str=""; awareness:float=0.0; pressure:float=0.0
    threshold:float=5.0; momentum:float=0.0; phase:str="INPUT"; digest:str=""

class Somataform:
    def __init__(self): self.state=State()
    def digest(self):
        data=asdict(self.state); data["digest"]=""
        self.state.digest=hashlib.sha256(json.dumps(data,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    def cycle(self, signal_value):
        s=self.state; s.cycle+=1; s.phase="INPUT"; s.signal=str(signal_value)
        s.phase="AWARENESS"; s.awareness=min(10.0,len(s.signal)/8.0)
        s.phase="THRESHOLD"; s.pressure += 1.0+s.awareness*0.5
        crossed=s.pressure>=s.threshold
        s.phase="ACTION"
        if crossed: s.momentum+=s.pressure; s.pressure=0.0
        else: s.momentum+=0.5
        s.phase="FEEDBACK"; s.pressure*=0.5; s.momentum*=0.9
        if s.cycle%7==0:
            s.phase="RECALIBRATION"
            s.threshold=max(1.0,min(10.0,5.0+s.awareness-s.momentum*0.1))
        s.phase="DIGEST"; self.digest()
        return {"cycle":s.cycle,"crossed":crossed,"state":asdict(s)}

class XMRigDispatcher:
    def __init__(self): self.process=None
    def start(self):
        if self.process is not None: return
        cmd=[XMRIG_PATH,"--config",XMRIG_CONFIG]
        print("\nStarting XMRig:\n"+" ".join(cmd)+"\n")
        self.process=subprocess.Popen(cmd)
    def running(self): return self.process is not None and self.process.poll() is None
    def monitor(self):
        if self.process is None: return False
        if self.process.poll() is not None:
            print(f"XMRig exited with code {self.process.returncode}"); self.process=None; return False
        return True
    def stop(self):
        if not self.running(): self.process=None; return
        print("Stopping XMRig..."); self.process.terminate()
        try: self.process.wait(timeout=10)
        except subprocess.TimeoutExpired: self.process.kill(); self.process.wait()
        self.process=None

def main():
    soma=Somataform(); miner=XMRigDispatcher(); stopping=False
    def shutdown(signum, frame):
        nonlocal stopping; print(f"\nReceived signal {signum}"); stopping=True
    signal.signal(signal.SIGINT,shutdown)
    if hasattr(signal,"SIGTERM"): signal.signal(signal.SIGTERM,shutdown)
    miner.start(); current_signal="TRUTH-AWARENESS"
    try:
        for _ in range(CYCLES):
            if stopping: break
            result=soma.cycle(current_signal)
            print("\n"+"="*60+"\nCYTHANX SOMATAFORM\n"+"="*60)
            print(json.dumps(result,indent=2)); current_signal=result["state"]["digest"]
            if not miner.monitor() and not stopping:
                print("XMRig stopped; restarting worker..."); time.sleep(RESTART_DELAY); miner.start()
            time.sleep(INTERVAL)
    finally:
        miner.stop(); print("\nCYTHANX dispatcher stopped.")

if __name__=="__main__": main()
