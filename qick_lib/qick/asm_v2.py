import logging
import numpy as np
from collections import namedtuple, OrderedDict, defaultdict
from abc import ABC, abstractmethod

#from .tprocv2_compiler import tprocv2_compile
from .tprocv2_assembler import Assembler
from .qick_asm import AbsQickProgram, QickRegister

class Wave(namedtuple('Wave', ["freq", "phase", "env", "gain", "length", "conf"])):
    widths = [4, 4, 3, 4, 4, 2]
    def compile(self):
        # convert to bytes to get a 168-bit word (this is what actually ends up in the wave memory)
        rawbytes = b''.join([int(i).to_bytes(length=w, byteorder='little', signed=True) for i, w in zip(self, self.widths)])
        # pad with zero bytes to get the 256-bit word (this is the format for DMA transfers)
        paddedbytes = rawbytes[:11]+bytes(1)+rawbytes[11:]+bytes(10)
        # pack into a numpy array
        return np.frombuffer(paddedbytes, dtype=np.int32)

class AbsRegisterManager(ABC):
    """Generic class for managing registers that will be written to a tProc-controlled block (signal generator or readout).
    """
    def __init__(self, prog, tproc_ch, ch_name):
        self.prog = prog
        # the tProc output channel controlled by this manager
        self.tproc_ch = tproc_ch
        # the name of this block (for messages)
        self.ch_name = ch_name

    def add_pulse(self, name, kwargs):
        """Set pulse parameters.
        This is called by QickProgram.set_pulse_registers().

        Parameters
        ----------
        kwargs : dict
            Parameter values

        """
        # check the final param set for validity
        self.check_params(kwargs)
        pulse = self.params2pulse(kwargs)

        # register the pulse and waves with the program
        self.prog.pulses[name] = pulse
        pulse['wavenames'] = []
        for iWave, wave in enumerate(pulse['waves']):
            wavename = "%s_wave%d" % (name, iWave)
            self.prog.add_wave(wavename, wave)
            pulse['wavenames'].append(wavename)

    @abstractmethod
    def check_params(self, params):
        ...

    @abstractmethod
    def params2pulse(self, params):
        ...

class AbsGenManager(AbsRegisterManager):
    """Manages the envelope and pulse information for a signal generator channel.
    """
    PARAMS_REQUIRED = {}
    PARAMS_OPTIONAL = {}

    def __init__(self, prog, gen_ch):
        self.ch = gen_ch
        self.gencfg = prog.soccfg['gens'][gen_ch]
        tproc_ch = self.gencfg['tproc_ch']
        super().__init__(prog, tproc_ch, "generator %d"%(gen_ch))
        self.samps_per_clk = self.gencfg['samps_per_clk']

        # dictionary of defined pulse envelopes
        self.envelopes = prog.envelopes[gen_ch]
        # type and max absolute value for envelopes
        self.env_dtype = np.int16

        self.addr = 0

    def check_params(self, params):
        """Check whether the parameters defined for a pulse are supported and sufficient for this generator and pulse type.
        Raise an exception if there is a problem.

        Parameters
        ----------
        params : dict
            Parameter values

        """
        style = params['style']
        required = set(self.PARAMS_REQUIRED[style])
        allowed = required | set(self.PARAMS_OPTIONAL[style])
        defined = params.keys()
        if required - defined:
            raise RuntimeError("missing required pulse parameter(s)", required - defined)
        if defined - allowed:
            raise RuntimeError("unsupported pulse parameter(s)", defined - allowed)

    def add_envelope(self, name, idata, qdata):
        """Add a waveform to the list of envelope waveforms available for this channel.
        The I and Q arrays must be of equal length, and the length must be divisible by the samples-per-clock of this generator.

        Parameters
        ----------
        name : str
            Name for this waveform
        idata : array
            I values for this waveform
        qdata : array
            Q values for this waveform

        """
        length = [len(d) for d in [idata, qdata] if d is not None]
        if len(length)==0:
            raise RuntimeError("Error: no data argument was supplied")
        # if both arrays were defined, they must be the same length
        if len(length)>1 and length[0]!=length[1]:
            raise RuntimeError("Error: I and Q pulse lengths must be equal")
        length = length[0]

        if (length % self.samps_per_clk) != 0:
            raise RuntimeError("Error: pulse lengths must be an integer multiple of %d"%(self.samps_per_clk))
        data = np.zeros((length, 2), dtype=self.env_dtype)

        for i, d in enumerate([idata, qdata]):
            if d is not None:
                # range check
                if np.max(np.abs(d)) > self.gencfg['maxv']:
                    raise ValueError("max abs val of envelope (%d) exceeds limit (%d)" % (np.max(np.abs(d)), self.gencfg['maxv']))
                # copy data
                data[:,i] = np.round(d)

        self.envelopes[name] = {"data": data, "addr": self.addr}
        self.addr += length

    def cfg2reg(self, outsel, mode, stdysel, phrst):
        """Creates generator config register value, by setting flags.

        Parameters
        ----------
        outsel : str
        Selects the output source. The output is complex. Tables define envelopes for I and Q.
        The default is "product".

        * If "product", the output is the product of table and DDS.

        * If "dds", the output is the DDS only.

        * If "input", the output is from the table for the real part, and zeros for the imaginary part.

        * If "zero", the output is always zero.

        mode : str
        Selects whether the output is "oneshot" or "periodic". The default is "oneshot".

        stdysel : str
        Selects what value is output continuously by the signal generator after the generation of a pulse.
        The default is "zero".

        phrst : int
        If 1, it resets the phase coherent accumulator. The default is 0.

        * If "last", it is the last calculated sample of the pulse.

        * If "zero", it is a zero value.

        Returns
        -------
        int
        Compiled mode code in binary
        """
        if outsel is None: outsel = "product"
        if mode is None: mode = "oneshot"
        if stdysel is None: stdysel = "zero"
        if phrst is None: phrst = 0
        outsel_reg = {"product": 0, "dds": 1, "input": 2, "zero": 3}[outsel]
        mode_reg = {"oneshot": 0, "periodic": 1}[mode]
        stdysel_reg = {"last": 0, "zero": 1}[stdysel]
        return phrst*0b010000 + stdysel_reg*0b01000 + mode_reg*0b00100 + outsel_reg

class FullSpeedGenManager(AbsGenManager):
    """Manager for the full-speed (non-interpolated, non-muxed) signal generators.
    """
    PARAMS_REQUIRED = {'const': ['style', 'freq', 'phase', 'gain', 'length'],
            'arb': ['style', 'freq', 'phase', 'gain', 'envelope'],
            'flat_top': ['style', 'freq', 'phase', 'gain', 'length', 'envelope']}
    PARAMS_OPTIONAL = {'const': ['phrst', 'stdysel', 'mode'],
            'arb': ['phrst', 'stdysel', 'mode', 'outsel'],
            'flat_top': ['phrst', 'stdysel']}

    def params2wave(self, freqreg, phasereg, gainreg, lenreg, env=0, mode=None, outsel=None, stdysel=None, phrst=None):
        #lenreg = int(self.prog.us2cycles(gen_ch=self.ch, us=length))
        if lenreg >= 2**16 or lenreg < 3:
            raise RuntimeError("Pulse length of %d cycles is out of range (exceeds 16 bits, or less than 3) - use multiple pulses, or zero-pad the waveform" % (lenreg))
        confreg = self.cfg2reg(outsel=outsel, mode=mode, stdysel=stdysel, phrst=phrst)
        return Wave(freqreg, phasereg, env, gainreg, lenreg, confreg)

    def params2pulse(self, params):
        """Write whichever pulse registers are fully determined by the defined parameters.

        The following pulse styles are supported:

        * const: A constant (rectangular) pulse.
          There is no outsel setting for this pulse style; "dds" is always used.

        * arb: An arbitrary-envelope pulse.

        * flat_top: A flattop pulse with arbitrary ramps.
          The waveform is played in three segments: ramp up, flat, and ramp down.
          To use these pulses one should use add_pulse to add the ramp waveform which should go from 0 to maxamp and back down to zero with the up and down having the same length, the first half will be used as the ramp up and the second half will be used as the ramp down.

          If the waveform is not of even length, the middle sample will be skipped.
          It's recommended to use an even-length waveform with flat_top.

          There is no outsel setting for flat_top; the ramps always use "product" and the flat segment always uses "dds".
          There is no mode setting; it is always "oneshot".

        Parameters
        ----------
        params : dict
            Pulse parameters
        """
        wavepars = {k:params.get(k) for k in ['phrst', 'stdysel']}
        ro_ch=0 #TODO
        wavepars['freqreg'] = self.prog.freq2reg(gen_ch=self.ch, f=params['freq'], ro_ch=ro_ch)
        wavepars['phasereg'] = self.prog.deg2reg(gen_ch=self.ch, deg=params['phase'])
        wavepars['gainreg'] = int(params['gain']*self.gencfg['maxv']*self.gencfg['maxv_scale'])

        if 'envelope' in params:
            pinfo = self.envelopes[params['envelope']]
            env_length = pinfo['data'].shape[0] // self.samps_per_clk
            env_addr = pinfo['addr'] // self.samps_per_clk
        style = params['style']

        pulse = {}
        pulse['waves'] = []
        if style=='const':
            wavepars.update({k:params.get(k) for k in ['mode']})
            wavepars['outsel'] = 'dds'
            pulse['waves'].append(self.params2wave(**wavepars))
            pulse['lenreg'] = int(self.prog.us2cycles(gen_ch=self.ch, us=params['length']))
        elif style=='arb':
            wavepars.update({k:params.get(k) for k in ['mode', 'outsel']})
            wavepars['env'] = env_addr
            wavepars['lenreg'] = env_length
            pulse['waves'].append(self.params2wave(**wavepars))
            pulse['length'] = env_length

        return pulse
        """
        elif style=='arb':
            mc = self.get_mode_code(phrst=phrst, stdysel=stdysel, mode=mode, outsel=outsel, length=wfm_length)
            self.set_reg('mode', mc, f'phrst| stdysel | mode | | outsel = 0b{mc//2**16:>05b} | length = {mc % 2**16} ')
            self.next_pulse['regs'].append([self.prog._sreg_tproc(self.tproc_ch,x) for x in ['freq', 'phase', 'addr', 'gain', 'mode']])
            self.next_pulse['length'] = wfm_length
        elif style=='flat_top':
            # address for ramp-down
            self.set_reg('addr2', addr+(wfm_length+1)//2)
            # gain for flat segment
            self.set_reg('gain2', params['gain']//2)
            # mode for ramp up
            mc = self.get_mode_code(phrst=phrst, stdysel=stdysel, mode='oneshot', outsel='product', length=wfm_length//2)
            self.set_reg('mode2', mc, f'phrst| stdysel | mode | | outsel = 0b{mc//2**16:>05b} | length = {mc % 2**16} ')
            # mode for flat segment
            mc = self.get_mode_code(phrst=False, stdysel=stdysel, mode='oneshot', outsel='dds', length=params['length'])
            self.set_reg('mode', mc, f'phrst| stdysel | mode | | outsel = 0b{mc//2**16:>05b} | length = {mc % 2**16} ')
            # mode for ramp down
            mc = self.get_mode_code(phrst=False, stdysel=stdysel, mode='oneshot', outsel='product', length=wfm_length//2)
            self.set_reg('mode3', mc, f'phrst| stdysel | mode | | outsel = 0b{mc//2**16:>05b} | length = {mc % 2**16} ')

            self.next_pulse['regs'].append([self.prog._sreg_tproc(self.tproc_ch,x) for x in ['freq', 'phase', 'addr', 'gain', 'mode2']])
            self.next_pulse['regs'].append([self.prog._sreg_tproc(self.tproc_ch,x) for x in ['freq', 'phase', '0', 'gain2', 'mode']])
            self.next_pulse['regs'].append([self.prog._sreg_tproc(self.tproc_ch,x) for x in ['freq', 'phase', 'addr2', 'gain', 'mode3']])
            self.next_pulse['length'] = (wfm_length//2)*2 + params['length']
        """

class QickProgramV2(AbsQickProgram):
    gentypes = {'axis_signal_gen_v4': FullSpeedGenManager,
                'axis_signal_gen_v5': FullSpeedGenManager,
                'axis_signal_gen_v6': FullSpeedGenManager}

    def __init__(self, soccfg):
        super().__init__(soccfg)
        self.prog_list = []
        self.labels = {'s15': 's15'} # register 15 predefinition

        # address in program memory
        self.p_addr = 1
        # line number
        self.line = 1
        # first instruction is always NOP, so both counters start at 1

        self.user_reg_dict = {}  # look up dict for registers defined in each generator channel
        self._user_regs = []  # addr of all user defined registers

        self.loop_list = []
        self.loop_stack = []

        # waveforms, to be written to the wave memory
        self.waves = OrderedDict()
        self.wave2idx = {}

        # pulses are software constructs, each is a set of 1 or more waveforms
        self.pulses = {}

        self._gen_mgrs = [self.gentypes[ch['type']](self, iCh) for iCh, ch in enumerate(soccfg['gens'])]

    def add_instruction(self, inst, addr_inc=1):
        # copy the instruction dict in case it's getting reused and modified
        inst = inst.copy()
        inst['P_ADDR'] = self.p_addr
        inst['LINE'] = self.line
        self.p_addr += addr_inc
        self.line += 1
        self.prog_list.append(inst)

    def end(self):
        self.add_instruction({'CMD':'JUMP', 'ADDR':f'&{self.p_addr}', 'UF':'0'})

    def wait(self, time):
        # the assembler translates "WAIT" into two instructions
        self.add_instruction({'CMD':'WAIT', 'ADDR':f'&{self.p_addr + 1}', 'TIME': f'{time}'}, addr_inc=2)

    def add_label(self, label):
        """apply the specified label to the next instruction
        """
        self.labels[label] = '&' + str(len(self.prog_list)+1)

    def new_reg(self, addr: int = None, name: str = None, init_val=None, reg_type: str = None,
                gen_ch: int = None, ro_ch: int = None):
        """ Declare a new data register.

        :param page: register page
        :param addr: address of the new register. If None, the function will automatically try to find the next
            available address.
        :param name: name of the new register. Optional.
        :param init_val: initial value for the register, when reg_type is provided, the reg_val should be in the
            physical unit of the corresponding type. i.e. freq in MHz, time in us, phase in deg.
        :param reg_type: {"freq", "time", "phase", "adc_freq"} or None, type of the register
        :param gen_ch: generator channel numer to which the register is associated with, for unit convert.
        :param ro_ch: readout channel numer to which the register is associated with, for unit convert.
        :return: QickRegister
        """
        if addr is None:
            addr = 0
            while addr in self._user_regs:
                addr += 1
            if addr >= self.soccfg['tprocs'][0]['dreg_qty']:
                raise RuntimeError(f"data registers are full.")
        else:
            if addr < 0 or addr >= self.soccfg['tprocs'][0]['dreg_qty']:
                raise ValueError(f"register address must be smaller than {self.soccfg['tprocs'][0]['dreg_qty']}")
            if addr in self._user_regs:
                raise ValueError(f"register at address {addr} is already occupied.")
        self._user_regs.append(addr)

        if name is None:
            name = f"reg_page{addr}"
        if name in self.user_reg_dict.keys():
            raise NameError(f"register name '{name}' already exists")

        reg = QickRegister(self, 0, addr, reg_type, gen_ch, ro_ch, init_val, name=name)
        self.user_reg_dict[name] = reg

        return reg
    

    def add_gauss(self, ch, name, sigma, length, maxv=None, even_length=False):
        """Adds a Gaussian pulse to the waveform library.
        The pulse will peak at length/2.

        Parameters
        ----------
        ch : int
            generator channel (index in 'gens' list)
        name : str
            Name of the pulse
        sigma : float
            Standard deviation of the Gaussian (in units of us)
        length : int
            Total pulse length (in units of us)
        maxv : float
            Value at the peak (if None, the max value for this generator will be used)
        """
        if even_length:
            lenreg = 2*self.us2cycles(gen_ch=ch, us=length/2)
        else:
            lenreg = self.us2cycles(gen_ch=ch, us=length)
        sigreg = self.us2cycles(gen_ch=ch, us=sigma)
        super().add_gauss(ch, name, sigreg, lenreg, maxv)

    def add_wave(self, name, wave):
        self.waves[name] = wave
        self.wave2idx[name] = len(self.waves)-1
        
    def add_pulse(self, ch, name, **kwargs):
        self._gen_mgrs[ch].add_pulse(name, kwargs)

    def pulse(self, ch, name, t=0):
        pulse = self.pulses[name]
        pulse_length = pulse['length']
        pulse_length *= self.tproccfg['f_time']/self.soccfg['gens'][ch]['f_fabric']
        ts = self.get_timestamp(gen_ch=ch)
        if t == 'auto':
            t = int(ts) #TODO: 0?
            self.set_timestamp(int(ts + pulse_length), gen_ch=ch)
        else:
            if t<ts:
                print("warning: pulse time %d appears to conflict with previous pulse ending at %f?"%(t, ts))
                self.set_timestamp(int(ts + pulse_length), gen_ch=ch)
            else:
                self.set_timestamp(int(t + pulse_length), gen_ch=ch)
        
        tproc_ch = ch # TODO: actually translate
        self.add_instruction({'CMD':"REG_WR", 'DST':'s14' ,'SRC':'imm' ,'LIT':str(t), 'UF':'0'})
        for wavename in pulse['wavenames']:
            idx = self.wave2idx[wavename]
            self.add_instruction({'CMD':'WPORT_WR', 'DST':str(tproc_ch) ,'SRC':'wmem', 'ADDR':'&'+str(idx), 'UF':'0'})

    def open_loop(self, n, name=None, addr=None):
        if name is None: name = f"loop_{len(self.loop_list)}"
        reg = self.new_reg(name=name, addr=addr)
        self.loop_list.append(name)
        self.loop_stack.append(name)
        self.add_instruction({'CMD':"REG_WR" , 'DST':'r'+str(reg.addr) ,'SRC':'imm' ,'LIT': str(n), 'UF':'0'})
        self.add_label(name.upper())
    
    def close_loop(self):
        name = self.loop_stack.pop()
        reg = self.user_reg_dict[name]
        self.add_instruction({'CMD':'REG_WR', 'DST':f'r{reg.addr}', 'SRC':'op', 'OP':f'r{reg.addr}-#1', 'UF':'1'})
        self.add_instruction({'CMD':'JUMP', 'LABEL':name.upper(), 'IF':'NZ', 'UF':'0'})
        
#         self.add_instruction({'CMD':'JUMP', 'LABEL':name.upper(), 'IF':'NZ', 'WR':f'r{reg.addr} op', 'OP':f'r{reg.addr}-#1', 'UF':'1' })
    
    def trigger(self, ros=None, pins=None, t=0, width=10):
        treg = self.us2cycles(t)
        #TODO: add DDR4+MR buffers, ADC offset
        if ros is None: ros = []
        if pins is None: pins = []
        outdict = defaultdict(int)
        trigset = set()
        for ro in ros:
            rocfg = self.soccfg['readouts'][ro]
            if rocfg['trigger_type'] == 'dport':
                outdict[rocfg['trigger_port']] |= (1 << rocfg['trigger_bit'])
            else:
                trigset.add(rocfg['trigger_port'])
            ts = self.get_timestamp(ro_ch=ro)
            if treg < ts: print("Readout time %d appears to conflict with previous readout ending at %f?"%(tireg, ts))
            ro_length = self.ro_chs[ro]['length']
            ro_length *= self.tproccfg['f_time']/self.soccfg['readouts'][ro]['f_fabric']
            self.set_timestamp(int(treg + ro_length), ro_ch=ro)
        for pin in pins:
            porttype, portnum, pinnum, _ = self.soccfg['tprocs'][0]['output_pins'][pin]
            if porttype == 'dport':
                outdict[portnum] |= (1 << pinnum)
            else:
                trigset.add(portnum)

        if outdict:
            self.add_instruction({'CMD':"REG_WR", 'DST':'s14', 'SRC':'imm', 'LIT': str(treg), 'UF':'0'})
            for outport, out in outdict.items():
                self.add_instruction({'CMD':'DPORT_WR', 'DST':str(outport), 'SRC':'imm', 'DATA':str(out), 'UF':'0'})
            self.add_instruction({'CMD':"REG_WR", 'DST':'s14','SRC':'imm', 'LIT':str(treg+width), 'UF':'0'})
            for outport, out in outdict.items():
                self.add_instruction({'CMD':'DPORT_WR', 'DST':str(outport), 'SRC':'imm', 'DATA':'0', 'UF':'0'})
        if trigset:
            for outport in trigset:
                self.add_instruction({'CMD':'TRIG', 'SRC':'set', 'DST':str(outport), 'TIME':str(treg)})
                self.add_instruction({'CMD':'TRIG', 'SRC':'clr', 'DST':str(outport), 'TIME':str(treg+width)})

    def declare_readout(self, ch, length, freq=None, sel='product', gen_ch=None):
        lenreg = self.us2cycles(ro_ch=ch, us=length)
        super().declare_readout(ch, lenreg, freq, sel, gen_ch)

    def sync_all(self, t=0):
        treg = self.us2cycles(t)
        max_t = self.get_max_timestamp()
        if max_t+treg > 0:
            self.add_instruction({'CMD':'TIME', 'DST':'inc_ref', 'LIT':f'{int(max_t+treg)}'})
            self.reset_timestamps()

    def wait_all(self, t=0):
        treg = self.us2cycles(t)
        self.wait(int(self.get_max_timestamp(gens=False, ros=True) + treg))

    def compile_prog(self):
        _, p_mem = Assembler.list2bin(self.prog_list, self.labels)
        return p_mem

    def compile_waves(self):
        if self.waves:
            return np.stack([w.compile() for w in self.waves.values()])
        else:
            return np.zeros((0,8), dtype=np.int32)

    def asm(self):
        asm = Assembler.list2asm(self.prog_list, self.labels)
        return asm

    def config_all(self, soc):
        soc.tproc.proc_stop()
        super().config_all(soc)
        soc.tproc.Load_PMEM(self.compile_prog())
        soc.tproc.load_mem(3, self.compile_waves())
