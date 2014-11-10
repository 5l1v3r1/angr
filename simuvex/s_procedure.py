#!/usr/bin/env python

import inspect
import itertools

import logging
l = logging.getLogger(name = "simuvex.s_procedure")

import claripy

symbolic_count = itertools.count()

from .s_run import SimRun
run_args = inspect.getargspec(SimRun.__init__)[0]

class SimProcedure(SimRun):
    ADDS_EXITS = False
    NO_RET = False

    def __init__(self, state, ret_expr=None, stmt_from=None, convention=None, arguments=None, sim_kwargs=None, **kwargs):
        self.kwargs = { } if sim_kwargs is None else sim_kwargs
        for a in kwargs.keys():
            if a not in run_args:
                l.warning("Argument '%s' passed to %s in **kwargs. Should be in sim_args.", a, self.__class__.__name__)
                self.kwargs[a] = kwargs.pop(a)

        SimRun.__init__(self, state, **kwargs)

        self.stmt_from = -1 if stmt_from is None else stmt_from
        self.convention = None
        self.set_convention(convention)
        self.arguments = arguments
        self.ret_expr = ret_expr
        self.symbolic_return = False
        self.state.sim_procedure = self.__class__.__name__

        # types
        self.argument_types = { } # a dictionary of index-to-type (i.e., type of arg 0: SimTypeString())
        self.return_type = None

        # prepare and analyze!
        if arguments is not None:
            self.state.options.add(o.AST_DEPS)
            self.state.options.add(o.AUTO_REFS)

        r = self.analyze(**self.kwargs)
        if r is not None:
            self.ret(r)

        if arguments is not None:
            self.state.options.discard(o.AST_DEPS)
            self.state.options.discard(o.AUTO_REFS)

    def analyze(self, **kwargs): #pylint:disable=unused-argument
        raise SimProcedureError("%s does not implement an analyze() method" % self.__class__.__name__)

    def reanalyze(self, new_state=None, addr=None, stmt_from=None, convention=None):
        new_state = self.initial_state.copy() if new_state is None else new_state
        addr = self.addr if addr is None else addr
        stmt_from = self.stmt_from if stmt_from is None else stmt_from
        convention = self.convention if convention is None else convention

        return self.__class__(new_state, addr=addr, stmt_from=stmt_from, convention=convention, **self.kwargs) #pylint:disable=E1124,E1123

    def initialize_run(self):
        pass

    def handle_run(self):
        self.handle_procedure()

    def handle_procedure(self):
        raise Exception("SimProcedure.handle_procedure() has been called. This should have been overwritten in class %s.", self.__class__)

    def set_convention(self, convention=None):
        if convention is None:
            if self.state.arch.name == "AMD64":
                convention = "systemv_x64"
            elif self.state.arch.name == "X86":
                convention = "cdecl"
            elif self.state.arch.name == "ARM":
                convention = "arm"
            elif self.state.arch.name == "MIPS":
                convention = "os2_mips"
            elif self.state.arch.name == "PPC32":
                convention = "ppc"
            elif self.state.arch.name == "PPC64":
                convention = "ppc"
            elif self.state.arch.name == "MIPS32":
                convention = "mips"

        self.convention = convention

    # Helper function to get an argument, given a list of register locations it can be and stack information for overflows.
    def arg_getter(self, reg_offsets, args_mem_base, stack_step, index):
        if index < len(reg_offsets):
            expr = self.state.reg_expr(reg_offsets[index], endness=self.state.arch.register_endness)
        else:
            index -= len(reg_offsets)
            mem_addr = args_mem_base + (index * stack_step)
            expr = self.state.mem_expr(mem_addr, stack_step, endness=self.state.arch.memory_endness)

        return expr

    def arg_setter(self, expr, reg_offsets, args_mem_base, stack_step, index):
        # Set register parameters
        if index < len(reg_offsets):
            offs = reg_offsets[index]
            self.state.store_reg(offs, expr, endness=self.state.arch.register_endness)

        # Set remaining parameters on the stack
        else:
            index -= len(reg_offsets)
            mem_addr = args_mem_base + (index * stack_step)
            self.state.store_mem(mem_addr, expr, endness=self.state.arch.memory_endness)

    def arg_reg_offsets(self):
        if self.convention == "cdecl" and self.state.arch.name == "X86":
            reg_offsets = [ ] # all on stack
        elif self.convention == "systemv_x64" and self.state.arch.name == "AMD64":
            reg_offsets = [ 72, 64, 32, 24, 80, 88 ] # rdi, rsi, rdx, rcx, r8, r9
        elif self.convention == "syscall" and self.state.arch.name == "AMD64":
            reg_offsets = [ 72, 64, 32, 24, 80, 88 ] # rdi, rsi, rdx, rcx, r8, r9
        elif self.convention == "arm" and self.state.arch.name == "ARM":
            reg_offsets = [ 8, 12, 16, 20 ] # r0, r1, r2, r3
        elif self.convention == "ppc" and self.state.arch.name == "PPC32":
            reg_offsets = [ 28, 32, 36, 40, 44, 48, 52, 56 ] # r3 through r10
        elif self.convention == "ppc" and self.state.arch.name == "PPC64":
            reg_offsets = [ 40, 48, 56, 64, 72, 80, 88, 96 ] # r3 through r10
        elif self.convention == "mips" and self.state.arch.name == "MIPS32":
            reg_offsets = [ 'a0', 'a1', 'a2', 'a3' ] # r4 through r7
        else:
            raise SimProcedureError("Unsupported arch %s and calling convention %s for getting register offsets", self.state.arch.name, self.convention)
        return reg_offsets

    def set_args(self, args):
        """
        Sets the value @expr as being the @index-th argument of a function
        """
        bv_args = [ ]
        for expr in args:
            if type(expr) in (int, long):
                e = self.state.BVV(expr, self.state.arch.bits)
            elif type(expr) in (str,):
                e = self.state.BVV(expr)
            elif not isinstance(expr, claripy.A):
                raise SimProcedureError("can't set argument of type %s" % type(expr))
            else:
                e = expr

            if len(e) != self.state.arch.bits:
                raise SimProcedureError("all args must be %d bits long" % self.state.arch.bits)

            bv_args.append(e)

        reg_offsets = self.arg_reg_offsets()
        stack_shift = (len(args) - len(reg_offsets)) * self.state.arch.stack_change
        sp_value = self.state.reg_expr('sp') + stack_shift
        self.state.store_reg('sp', sp_value)

        for index,e in reversed(tuple(enumerate(bv_args))):
            self.arg_setter(e, reg_offsets, sp_value, stack_shift, index)

    # Returns a bitvector expression representing the nth argument of a function
    def arg(self, index):
        if self.arguments is not None:
            return self.arguments[index]

        if self.convention in ("systemv_x64", "syscall") and self.state.arch.name == "AMD64":
            reg_offsets = self.arg_reg_offsets()
            return self.arg_getter(reg_offsets, self.state.reg_expr(self.state.arch.sp_offset) + 8, 8, index)
        elif self.convention == "cdecl" and self.state.arch.name == "X86":
            reg_offsets = self.arg_reg_offsets()
            return self.arg_getter(reg_offsets, self.state.reg_expr(self.state.arch.sp_offset) + 4, 4, index)
        elif self.convention == "arm" and self.state.arch.name == "ARM":
            # TODO: verify and make configurable
            reg_offsets = self.arg_reg_offsets()
            return self.arg_getter(reg_offsets, self.state.reg_expr(self.state.arch.sp_offset), 4, index)
        elif self.convention == "ppc" and self.state.arch.name == "PPC32":
            reg_offsets = self.arg_reg_offsets()
            # TODO: figure out how to get at the other arguments (I think they're just passed on the stack)
            return self.arg_getter(reg_offsets, None, 4, index)
        elif self.convention == "ppc" and self.state.arch.name == "PPC64":
            reg_offsets = self.arg_reg_offsets()
            # TODO: figure out how to get at the other arguments (I think they're just passed on the stack)
            return self.arg_getter(reg_offsets, None, 8, index)
        elif self.convention == "mips" and self.state.arch.name == "MIPS32":
            reg_offsets = self.arg_reg_offsets()
            return self.arg_getter(reg_offsets, self.state.reg_expr(116), 4, index)

        raise SimProcedureError("Unsupported calling convention %s for arguments" % self.convention)

    def inline_call(self, procedure, *arguments, **sim_kwargs):
        e_args = [ self.state.BVV(a, self.state.arch.bits) if type(a) in (int, long) else a for a in arguments ]
        p = procedure(self.state, inline=True, arguments=e_args, sim_kwargs=sim_kwargs)
        self.copy_actions(p)
        return p

    # Sets an expression as the return value. Also updates state.
    def set_return_expr(self, expr):
        if type(expr) in (int, long):
            expr = self.state.BVV(expr, self.state.arch.bits)

        if o.SIMPLIFY_RETS in self.state.options:
            l.debug("... simplifying")
            l.debug("... before: %s", expr)
            expr = self.state.se.simplify(expr)
            l.debug("... after: %s", expr)

        if self.symbolic_return:
            size = len(expr)
            new_expr = self.state.BV("multiwrite_" + self.__class__.__name__, size) #pylint:disable=maybe-no-member
            self.state.add_constraints(new_expr == expr)
            expr = new_expr

        if self.arguments is not None:
            self.ret_expr = expr
            return

        if self.state.arch.name == "AMD64":
            self.state.store_reg(16, expr)
        elif self.state.arch.name == "X86":
            self.state.store_reg(8, expr)
        elif self.state.arch.name == "ARM":
            self.state.store_reg(8, expr)
        elif self.state.arch.name == "PPC32":
            self.state.store_reg(28, expr)
        elif self.state.arch.name == "PPC64":
            self.state.store_reg(40, expr)
        elif self.state.arch.name == "MIPS32":
            self.state.store_reg(8, expr)
        else:
            raise SimProcedureError("Unsupported architecture %s for returns" % self.state.arch)

    # Adds an exit representing the function returning. Modifies the state.
    def ret(self, expr=None):
        if expr is not None: self.set_return_expr(expr)
        if self.arguments is not None:
            l.debug("Returning without setting exits due to 'internal' call.")
            return

        if self.ret_expr is None:
            ret_irsb = self.state.arch.get_ret_irsb(self.addr)
            ret_sirsb = SimIRSB(self.state, ret_irsb, addr=self.addr) #pylint:disable=E1123
            self.copy_exits(ret_sirsb)
            self.copy_actions(ret_sirsb)
        else:
            e = SimExit(expr=self.ret_expr, source=self.addr, state=self.state, jumpkind="Ijk_Ret")
            self.add_exits(e)

    def add_exits(self, *exits):
        for e in exits:
            e.state.options.discard(o.AST_DEPS)
            e.guard = _raw_ast(e.guard, {})
            e.target = _raw_ast(e.target, {})
        SimRun.add_exits(self, *exits)

    def ty_ptr(self, ty):
        return SimTypePointer(self.state.arch, ty)

    def __repr__(self):
        if self._custom_name is not None:
            return "<SimProcedure %s>" % self._custom_name
        else:
            return "<SimProcedure %s>" % self.__class__.__name__

from . import s_options as o
from .s_errors import SimProcedureError
from .s_irsb import SimIRSB
from .s_type import SimTypePointer
from .s_exit import SimExit
from .s_ast import _raw_ast
