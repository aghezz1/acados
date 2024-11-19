#
# Copyright (c) The acados authors.
#
# This file is part of acados.
#
# The 2-Clause BSD License
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.;
#

from typing import Union, List, Optional

import os
import casadi as ca
from .utils import is_empty, casadi_length, check_casadi_version_supports_p_global, print_casadi_expression
from .acados_model import AcadosModel
from .acados_ocp_constraints import AcadosOcpConstraints


def get_casadi_symbol(x):
    if isinstance(x, ca.MX):
        return ca.MX.sym
    elif isinstance(x, ca.SX):
        return ca.SX.sym
    else:
        raise TypeError("Expected casadi SX or MX.")

def is_casadi_SX(x):
    if isinstance(x, ca.SX):
        return True
    return False


class GenerateContext:
    def __init__(self, p_global: Optional[Union[ca.SX, ca.MX]], problem_name: str, opts=None):
        self.p_global = p_global
        if p_global is not None:
            check_casadi_version_supports_p_global()

        self.problem_name = problem_name

        self.opts = opts
        self.casadi_codegen_opts = dict(mex=False, casadi_int='int', casadi_real='double')
        self.list_funname_dir_pairs = []  # list of (function_name, output_dir)

        self.generic_funname_dir_pairs = []  # list of (function_name, output_dir) of functions that are not generated by acados
        self.function_input_output_pairs: List[List[Union[ca.SX, ca.MX], Union[ca.SX, ca.MX]]] = []

        self.global_data_sym = None
        self.global_data_expr = None

        # check if CasADi version supports cse
        try:
            from casadi import cse
            casadi_fun_opts = {"cse": True}
        except:
            print("NOTE: Please consider updating to CasADi 3.6.6 which supports common subexpression elimination. \nThis might speed up external function evaluation.")
            casadi_fun_opts = {}

        self.__casadi_fun_opts = casadi_fun_opts


    def __generate_functions(self):
        for (name, output_dir), (inputs, outputs) in zip(self.list_funname_dir_pairs, self.function_input_output_pairs):
            # create function
            try:
                fun = ca.Function(name, inputs, outputs, self.__casadi_fun_opts)
                # print(f"Generating function {name} with inputs {inputs}")
            except Exception as e:
                print(f"\nError while creating function {name} with inputs {inputs} and outputs {outputs}")
                print(e)
                raise e

            # setup and change directory
            cwd = os.getcwd()
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)
            os.chdir(output_dir)

            # generate function
            try:
                fun.generate(name, self.casadi_codegen_opts)
            except Exception as e:
                print(f"Error while generating function {name} in directory {output_dir}")
                print(e)
                raise e

            # change back to original directory
            os.chdir(cwd)

    def add_external_function_file(self, fun_name: str, output_dir: str):
        # remove trailing .c if present
        if fun_name.endswith(".c"):
            fun_name = fun_name[:-2]
        self.generic_funname_dir_pairs.append((fun_name, output_dir))

    def add_function_definition(self,
                                name: str,
                                inputs: List[Union[ca.MX, ca.SX]],
                                outputs: List[Union[ca.MX, ca.SX]],
                                output_dir: str):
        self.list_funname_dir_pairs.append((name, output_dir))
        self.function_input_output_pairs.append([inputs, outputs])

    def __setup_p_global_precompute_fun(self):
        precompute_pairs = []
        for i in range(len(self.function_input_output_pairs)):
            outputs = ca.cse(self.function_input_output_pairs[i][1])

            # detect parametric expressions in p_global
            [outputs_ret, symbols, param_expr] = ca.extract_parametric(outputs, self.p_global)

            # substitute previously detected param_expr in outputs
            symbols_to_add = []
            param_expr_to_add = []
            for sym_new, expr_new in zip(symbols, param_expr):
                add = True
                for sym, expr in precompute_pairs:
                    if ca.is_equal(expr, expr_new):
                        outputs_ret = [ca.substitute(output, sym_new, sym) for output in outputs_ret]
                        add = False
                        break
                if add:
                    symbols_to_add.append(sym_new)
                    param_expr_to_add.append(expr_new)

            # replace output expression with ones that use extracted expressions
            self.function_input_output_pairs[i][1] = outputs_ret
            # store (new input symbols, extracted expressions)
            for sym, expr in zip(symbols_to_add, param_expr_to_add):
                precompute_pairs.append([sym, expr])

        global_data_sym_list = [input for input, _ in precompute_pairs]
        self.global_data_sym = ca.vertcat(*global_data_sym_list)
        self.global_data_expr = ca.cse(ca.vertcat(*[output for _, output in precompute_pairs]))

        # add global data as input to all functions
        for i in range(len(self.function_input_output_pairs)):
            self.function_input_output_pairs[i][0].append(self.global_data_sym)

        output_dir = os.path.abspath(self.opts["code_export_directory"])
        fun_name = f'{self.problem_name}_p_global_precompute_fun'
        self.add_function_definition(fun_name, [self.p_global], [self.global_data_expr], output_dir)

        # self.print_global_data_summary()

        assert casadi_length(self.global_data_expr) == casadi_length(self.global_data_sym), f"Length mismatch: {casadi_length(self.global_data_expr)} != {casadi_length(self.global_data_sym)}"

    def finalize(self):
        if self.p_global is not None:
            self.__setup_p_global_precompute_fun()

        self.__generate_functions()
        return

    def get_n_global_data(self):
        return casadi_length(self.global_data_sym)

    def get_external_function_file_list(self, ocp_specific=False):
        out = []
        for (fun_name, fun_dir) in self.generic_funname_dir_pairs + self.list_funname_dir_pairs:
            rel_fun_dir = os.path.relpath(fun_dir, self.opts["code_export_directory"])
            is_ocp_specific = not rel_fun_dir.endswith("model")
            if ocp_specific != is_ocp_specific:
                continue
            out.append(f"{rel_fun_dir}/{fun_name}.c")
        return out

    def print_global_data_summary(self) -> None:
        if casadi_length(self.global_data_expr) == 0:
            print("\nGenerateContext: detected empty global_data_expr.")
        else:
            print("\nGenerateContext: detected global_data_expr:\n")
            print_casadi_expression(self.global_data_expr)

################
# Dynamics
################

def generate_c_code_discrete_dynamics(context: GenerateContext, model: AcadosModel, model_dir: str):
    opts = context.opts

    # load model
    x = model.x
    u = model.u
    p = model.p
    p_global = model.p_global
    phi = model.disc_dyn_expr
    model_name = model.name

    symbol = get_casadi_symbol(x)
    nx1 = casadi_length(phi)

    lam = symbol('lam', nx1, 1)
    ux = ca.vertcat(u, x)

    if model.disc_dyn_custom_jac is not None:
        # use custom jacobians
        jac_ux = model.disc_dyn_custom_jac
    else:
        # generate jacobians
        jac_ux = ca.jacobian(phi, ux)

    # generate adjoint
    adj_ux = ca.jtimes(phi, ux, lam, True)
    # generate hessian
    hess_ux = ca.jacobian(adj_ux, ux, {"symmetric": is_casadi_SX(x)})

    # set up & generate ca.Functions
    fun_name = model_name + '_dyn_disc_phi_fun'
    context.add_function_definition(fun_name, [x, u, p], [phi], model_dir)

    fun_name = model_name + '_dyn_disc_phi_fun_jac'
    context.add_function_definition(fun_name, [x, u, p], [phi, jac_ux.T], model_dir)

    fun_name = model_name + '_dyn_disc_phi_fun_jac_hess'
    context.add_function_definition(fun_name, [x, u, lam, p], [phi, jac_ux.T, hess_ux], model_dir)

    if opts["with_solution_sens_wrt_params"]:
        # generate jacobian of lagrange gradient wrt p
        jac_p = ca.jacobian(phi, p_global)
        # hess_xu_p_old = ca.jacobian((lam.T @ jac_ux).T, p)
        hess_xu_p = ca.jacobian(adj_ux, p_global) # using adjoint
        fun_name = model_name + '_dyn_disc_phi_jac_p_hess_xu_p'
        context.add_function_definition(fun_name, [x, u, lam, p], [jac_p, hess_xu_p], model_dir)

    if opts["with_value_sens_wrt_params"]:
        adj_p = ca.jtimes(phi, p_global, lam, True)
        fun_name = model_name + '_dyn_disc_phi_adj_p'
        context.add_function_definition(fun_name, [x, u, lam, p], [adj_p], model_dir)

    return



def generate_c_code_explicit_ode(context: GenerateContext, model: AcadosModel, model_dir: str):
    generate_hess = context.opts["generate_hess"]

    # load model
    x = model.x
    u = model.u
    p = model.p
    f_expl = model.f_expl_expr
    model_name = model.name

    nx = x.size()[0]
    nu = u.size()[0]

    symbol = get_casadi_symbol(x)

    # set up expressions
    Sx = symbol('Sx', nx, nx)
    Sp = symbol('Sp', nx, nu)
    lambdaX = symbol('lambdaX', nx, 1)

    vdeX = ca.jtimes(f_expl, x, Sx)
    vdeP = ca.jacobian(f_expl, u) + ca.jtimes(f_expl, x, Sp)
    adj = ca.jtimes(f_expl, ca.vertcat(x, u), lambdaX, True)

    if generate_hess:
        S_forw = ca.vertcat(ca.horzcat(Sx, Sp), ca.horzcat(ca.DM.zeros(nu,nx), ca.DM.eye(nu)))
        hess = ca.mtimes(ca.transpose(S_forw),ca.jtimes(adj, ca.vertcat(x,u), S_forw))
        hess2 = []
        for j in range(nx+nu):
            for i in range(j,nx+nu):
                hess2 = ca.vertcat(hess2, hess[i,j])

    # add to context
    fun_name = model_name + '_expl_ode_fun'
    context.add_function_definition(fun_name, [x, u, p], [f_expl], model_dir)

    fun_name = model_name + '_expl_vde_forw'
    context.add_function_definition(fun_name, [x, Sx, Sp, u, p], [f_expl, vdeX, vdeP], model_dir)

    fun_name = model_name + '_expl_vde_adj'
    context.add_function_definition(fun_name, [x, lambdaX, u, p], [adj], model_dir)

    if generate_hess:
        fun_name = model_name + '_expl_ode_hess'
        context.add_function_definition(fun_name, [x, Sx, Sp, lambdaX, u, p], [adj, hess2], model_dir)

    return


def generate_c_code_implicit_ode(context: GenerateContext, model: AcadosModel, model_dir: str):

    # load model
    x = model.x
    xdot = model.xdot
    u = model.u
    z = model.z
    p = model.p
    t = model.t
    f_impl = model.f_impl_expr
    model_name = model.name

    # get model dimensions
    nx = casadi_length(x)
    nz = casadi_length(z)

    # generate jacobians
    jac_x = ca.jacobian(f_impl, x)
    jac_xdot = ca.jacobian(f_impl, xdot)
    jac_u = ca.jacobian(f_impl, u)
    jac_z = ca.jacobian(f_impl, z)

    # Set up functions
    p = model.p
    fun_name = model_name + '_impl_dae_fun'
    context.add_function_definition(fun_name, [x, xdot, u, z, t, p], [f_impl], model_dir)

    fun_name = model_name + '_impl_dae_fun_jac_x_xdot_z'
    context.add_function_definition(fun_name, [x, xdot, u, z, t, p], [f_impl, jac_x, jac_xdot, jac_z], model_dir)

    fun_name = model_name + '_impl_dae_fun_jac_x_xdot_u_z'
    context.add_function_definition(fun_name, [x, xdot, u, z, t, p], [f_impl, jac_x, jac_xdot, jac_u, jac_z], model_dir)

    fun_name = model_name + '_impl_dae_fun_jac_x_xdot_u'
    context.add_function_definition(fun_name, [x, xdot, u, z, t, p], [f_impl, jac_x, jac_xdot, jac_u], model_dir)

    fun_name = model_name + '_impl_dae_jac_x_xdot_u_z'
    context.add_function_definition(fun_name, [x, xdot, u, z, t, p], [jac_x, jac_xdot, jac_u, jac_z], model_dir)

    if context.opts["generate_hess"]:
        x_xdot_z_u = ca.vertcat(x, xdot, z, u)
        symbol = get_casadi_symbol(x)
        multiplier = symbol('multiplier', nx + nz)
        ADJ = ca.jtimes(f_impl, x_xdot_z_u, multiplier, True)
        HESS = ca.jacobian(ADJ, x_xdot_z_u, {"symmetric": is_casadi_SX(x)})
        fun_name = model_name + '_impl_dae_hess'
        context.add_function_definition(fun_name, [x, xdot, u, z, multiplier, t, p], [HESS], model_dir)

    return


def generate_c_code_gnsf(context: GenerateContext, model: AcadosModel, model_dir: str):
    model_name = model.name

    # obtain gnsf dimensions
    get_matrices_fun = model.get_matrices_fun
    phi_fun = model.phi_fun

    size_gnsf_A = get_matrices_fun.size_out(0)
    gnsf_nx1 = size_gnsf_A[1]
    gnsf_nz1 = size_gnsf_A[0] - size_gnsf_A[1]
    gnsf_nuhat = max(phi_fun.size_in(1))
    gnsf_ny = max(phi_fun.size_in(0))

    # set up expressions
    # if the model uses ca.MX because of cost/constraints
    # the DAE can be exported as ca.SX -> detect GNSF in Matlab
    # -> evaluated ca.SX GNSF functions with ca.MX.
    u = model.u
    symbol = get_casadi_symbol(u)

    y = symbol("y", gnsf_ny, 1)
    uhat = symbol("uhat", gnsf_nuhat, 1)
    p = model.p
    x1 = symbol("gnsf_x1", gnsf_nx1, 1)
    x1dot = symbol("gnsf_x1dot", gnsf_nx1, 1)
    z1 = symbol("gnsf_z1", gnsf_nz1, 1)
    dummy = symbol("gnsf_dummy", 1, 1)
    empty_var = symbol("gnsf_empty_var", 0, 0)

    ## generate C code
    fun_name = model_name + '_gnsf_phi_fun'
    context.add_function_definition(fun_name, [y, uhat, p], [phi_fun(y, uhat, p)], model_dir)

    fun_name = model_name + '_gnsf_phi_fun_jac_y'
    phi_fun_jac_y = model.phi_fun_jac_y
    context.add_function_definition(fun_name, [y, uhat, p], phi_fun_jac_y(y, uhat, p), model_dir)

    fun_name = model_name + '_gnsf_phi_jac_y_uhat'
    phi_jac_y_uhat = model.phi_jac_y_uhat
    context.add_function_definition(fun_name, [y, uhat, p], phi_jac_y_uhat(y, uhat, p), model_dir)

    fun_name = model_name + '_gnsf_f_lo_fun_jac_x1k1uz'
    f_lo_fun_jac_x1k1uz = model.f_lo_fun_jac_x1k1uz
    f_lo_fun_jac_x1k1uz_eval = f_lo_fun_jac_x1k1uz(x1, x1dot, z1, u, p)

    # avoid codegeneration issue
    if not isinstance(f_lo_fun_jac_x1k1uz_eval, tuple) and is_empty(f_lo_fun_jac_x1k1uz_eval):
        f_lo_fun_jac_x1k1uz_eval = [empty_var]

    context.add_function_definition(fun_name, [x1, x1dot, z1, u, p], f_lo_fun_jac_x1k1uz_eval, model_dir)

    fun_name = model_name + '_gnsf_get_matrices_fun'
    context.add_function_definition(fun_name, [dummy], get_matrices_fun(1), model_dir)

    # remove fields for json dump
    del model.phi_fun
    del model.phi_fun_jac_y
    del model.phi_jac_y_uhat
    del model.f_lo_fun_jac_x1k1uz
    del model.get_matrices_fun

    return


################
# Cost
################

def generate_c_code_external_cost(context: GenerateContext, model: AcadosModel, stage_type):
    opts = context.opts

    x = model.x
    p = model.p
    u = model.u
    z = model.z
    p_global = model.p_global
    symbol = get_casadi_symbol(x)

    if stage_type == 'terminal':
        suffix_name = "_cost_ext_cost_e_fun"
        suffix_name_hess = "_cost_ext_cost_e_fun_jac_hess"
        suffix_name_jac = "_cost_ext_cost_e_fun_jac"
        suffix_name_param_sens = "_cost_ext_cost_e_hess_xu_p"
        suffix_name_value_sens = "_cost_ext_cost_e_grad_p"
        ext_cost = model.cost_expr_ext_cost_e
        custom_hess = model.cost_expr_ext_cost_custom_hess_e
        # Last stage cannot depend on u and z
        u = symbol("u", 0, 0)
        z = symbol("z", 0, 0)

    elif stage_type == 'path':
        suffix_name = "_cost_ext_cost_fun"
        suffix_name_hess = "_cost_ext_cost_fun_jac_hess"
        suffix_name_jac = "_cost_ext_cost_fun_jac"
        suffix_name_param_sens = "_cost_ext_cost_hess_xu_p"
        suffix_name_value_sens = "_cost_ext_cost_grad_p"
        ext_cost = model.cost_expr_ext_cost
        custom_hess = model.cost_expr_ext_cost_custom_hess

    elif stage_type == 'initial':
        suffix_name = "_cost_ext_cost_0_fun"
        suffix_name_hess = "_cost_ext_cost_0_fun_jac_hess"
        suffix_name_jac = "_cost_ext_cost_0_fun_jac"
        suffix_name_param_sens = "_cost_ext_cost_0_hess_xu_p"
        suffix_name_value_sens = "_cost_ext_cost_0_grad_p"
        ext_cost = model.cost_expr_ext_cost_0
        custom_hess = model.cost_expr_ext_cost_custom_hess_0

    nunx = x.shape[0] + u.shape[0]

    # set up functions to be exported
    fun_name = model.name + suffix_name
    fun_name_hess = model.name + suffix_name_hess
    fun_name_jac = model.name + suffix_name_jac
    fun_name_param = model.name + suffix_name_param_sens
    fun_name_value_sens = model.name + suffix_name_value_sens

    # generate expression for full gradient and Hessian
    hess_uxz, grad_uxz = ca.hessian(ext_cost, ca.vertcat(u, x, z))

    hess_ux = hess_uxz[:nunx, :nunx]
    hess_z = hess_uxz[nunx:, nunx:]
    hess_z_ux = hess_uxz[nunx:, :nunx]

    if custom_hess is not None:
        hess_ux = custom_hess

    cost_dir = os.path.abspath(os.path.join(opts["code_export_directory"], f'{model.name}_cost'))

    context.add_function_definition(fun_name, [x, u, z, p], [ext_cost], cost_dir)
    context.add_function_definition(fun_name_hess, [x, u, z, p], [ext_cost, grad_uxz, hess_ux, hess_z, hess_z_ux], cost_dir)
    context.add_function_definition(fun_name_jac, [x, u, z, p], [ext_cost, grad_uxz], cost_dir)

    if opts["with_solution_sens_wrt_params"]:
        if casadi_length(z) > 0:
            raise Exception("acados: solution sensitivities wrt parameters not supported with algebraic variables.")
        grad_ux = ca.jacobian(ext_cost, ca.vertcat(u, x))
        hess_xu_p = ca.jacobian(grad_ux, p_global)
        context.add_function_definition(fun_name_param, [x, u, z, p], [hess_xu_p], cost_dir)

    if opts["with_value_sens_wrt_params"]:
        grad_p = ca.jacobian(ext_cost, p_global).T
        context.add_function_definition(fun_name_value_sens, [x, u, z, p], [grad_p], cost_dir)

    return


def generate_c_code_nls_cost(context: GenerateContext, model: AcadosModel, stage_type):
    opts = context.opts

    x = model.x
    z = model.z
    p = model.p
    u = model.u
    t = model.t

    symbol = get_casadi_symbol(x)

    if stage_type == 'terminal':
        middle_name = '_cost_y_e'
        u = symbol('u', 0, 0)
        y_expr = model.cost_y_expr_e

    elif stage_type == 'initial':
        middle_name = '_cost_y_0'
        y_expr = model.cost_y_expr_0

    elif stage_type == 'path':
        middle_name = '_cost_y'
        y_expr = model.cost_y_expr

    cost_dir = os.path.abspath(os.path.join(opts["code_export_directory"], f'{model.name}_cost'))

    # set up expressions
    cost_jac_expr = ca.transpose(ca.jacobian(y_expr, ca.vertcat(u, x)))
    dy_dz = ca.jacobian(y_expr, z)
    ny = casadi_length(y_expr)

    # Check if dimension is 0, otherwise Casadi will crash
    y = symbol('y', ny, 1)
    if ny == 0:
        y_adj = 0
        y_hess = 0
    else:
        y_adj = ca.jtimes(y_expr, ca.vertcat(u, x), y, True)
        y_hess = ca.jacobian(y_adj, ca.vertcat(u, x), {"symmetric": is_casadi_SX(x)})

    ## generate C code
    suffix_name = '_fun'
    fun_name = model.name + middle_name + suffix_name
    context.add_function_definition(fun_name, [x, u, z, t, p], [ y_expr ], cost_dir)

    suffix_name = '_fun_jac_ut_xt'
    fun_name = model.name + middle_name + suffix_name
    context.add_function_definition(fun_name, [x, u, z, t, p], [ y_expr, cost_jac_expr, dy_dz ], cost_dir)

    suffix_name = '_hess'
    fun_name = model.name + middle_name + suffix_name
    context.add_function_definition(fun_name, [x, u, z, y, t, p], [ y_hess ], cost_dir)

    return



def generate_c_code_conl_cost(context: GenerateContext, model: AcadosModel, stage_type: str):

    opts = context.opts
    x = model.x
    z = model.z
    p = model.p
    p_global = model.p_global
    t = model.t

    symbol = get_casadi_symbol(x)
    if p_global is None:
        p_global = symbol('p_global', 0, 0)

    if stage_type == 'terminal':
        u = symbol('u', 0, 0)

        yref = model.cost_r_in_psi_expr_e
        inner_expr = model.cost_y_expr_e - yref
        outer_expr = model.cost_psi_expr_e
        res_expr = model.cost_r_in_psi_expr_e

        suffix_name_fun = '_conl_cost_e_fun'
        suffix_name_fun_jac_hess = '_conl_cost_e_fun_jac_hess'

        custom_hess = model.cost_conl_custom_outer_hess_e

    elif stage_type == 'initial':
        u = model.u

        yref = model.cost_r_in_psi_expr_0
        inner_expr = model.cost_y_expr_0 - yref
        outer_expr = model.cost_psi_expr_0
        res_expr = model.cost_r_in_psi_expr_0

        suffix_name_fun = '_conl_cost_0_fun'
        suffix_name_fun_jac_hess = '_conl_cost_0_fun_jac_hess'

        custom_hess = model.cost_conl_custom_outer_hess_0

    elif stage_type == 'path':
        u = model.u

        yref = model.cost_r_in_psi_expr
        inner_expr = model.cost_y_expr - yref
        outer_expr = model.cost_psi_expr
        res_expr = model.cost_r_in_psi_expr

        suffix_name_fun = '_conl_cost_fun'
        suffix_name_fun_jac_hess = '_conl_cost_fun_jac_hess'

        custom_hess = model.cost_conl_custom_outer_hess

    # set up function names
    fun_name_cost_fun = model.name + suffix_name_fun
    fun_name_cost_fun_jac_hess = model.name + suffix_name_fun_jac_hess

    # set up functions to be exported
    outer_loss_fun = ca.Function('psi', [res_expr, t, p, p_global], [outer_expr])
    cost_expr = outer_loss_fun(inner_expr, t, p, p_global)

    outer_loss_grad_fun = ca.Function('outer_loss_grad', [res_expr, t, p, p_global], [ca.jacobian(outer_expr, res_expr).T])

    if custom_hess is None:
        hess = ca.hessian(outer_loss_fun(res_expr, t, p, p_global), res_expr)[0]
    else:
        hess = custom_hess

    outer_hess_fun = ca.Function('outer_hess', [res_expr, t, p, p_global], [hess])
    outer_hess_expr = outer_hess_fun(inner_expr, t, p, p_global)
    outer_hess_is_diag = outer_hess_expr.sparsity().is_diag()
    if casadi_length(res_expr) <= 4:
        outer_hess_is_diag = 0

    Jt_ux_expr = ca.jacobian(inner_expr, ca.vertcat(u, x)).T
    Jt_z_expr = ca.jacobian(inner_expr, z).T

    # change directory
    cost_dir = os.path.abspath(os.path.join(opts["code_export_directory"], f'{model.name}_cost'))

    context.add_function_definition(
        fun_name_cost_fun,
        [x, u, z, yref, t, p],
        [cost_expr], cost_dir)

    context.add_function_definition(
        fun_name_cost_fun_jac_hess,
        [x, u, z, yref, t, p],
        [cost_expr, outer_loss_grad_fun(inner_expr, t, p, p_global), Jt_ux_expr, Jt_z_expr, outer_hess_expr, outer_hess_is_diag],
        cost_dir
    )

    return


################
# Constraints
################
def generate_c_code_constraint(context: GenerateContext, model: AcadosModel, constraints: AcadosOcpConstraints, stage_type: str):

    opts = context.opts

    # load constraint variables and expression
    x = model.x
    p = model.p
    u = model.u
    z = model.z

    symbol = get_casadi_symbol(x)

    if stage_type == 'terminal':
        constr_type = constraints.constr_type_e
        con_h_expr = model.con_h_expr_e
        con_phi_expr = model.con_phi_expr_e
        # create dummy u, z
        u = symbol('u', 0, 0)
        z = symbol('z', 0, 0)
    elif stage_type == 'initial':
        constr_type = constraints.constr_type_0
        con_h_expr = model.con_h_expr_0
        con_phi_expr = model.con_phi_expr_0
    elif stage_type == 'path':
        constr_type = constraints.constr_type
        con_h_expr = model.con_h_expr
        con_phi_expr = model.con_phi_expr

    if (not is_empty(con_h_expr)) and (not is_empty(con_phi_expr)):
        raise Exception("acados: you can either have constraint_h, or constraint_phi, not both.")

    if (is_empty(con_h_expr) and is_empty(con_phi_expr)):
        # both empty -> nothing to generate
        return

    if is_empty(p):
        p = symbol('p', 0, 0)

    if is_empty(z):
        z = symbol('z', 0, 0)

    # multipliers for hessian
    nh = casadi_length(con_h_expr)
    lam_h = symbol('lam_h', nh, 1)

    # directory
    constraints_dir = os.path.abspath(os.path.join(opts["code_export_directory"], f'{model.name}_constraints'))

    # export casadi functions
    if constr_type == 'BGH':
        if stage_type == 'terminal':
            fun_name = model.name + '_constr_h_e_fun_jac_uxt_zt'
        elif stage_type == 'initial':
            fun_name = model.name + '_constr_h_0_fun_jac_uxt_zt'
        else:
            fun_name = model.name + '_constr_h_fun_jac_uxt_zt'

        jac_ux_t = ca.transpose(ca.jacobian(con_h_expr, ca.vertcat(u,x)))
        jac_z_t = ca.jacobian(con_h_expr, z)
        context.add_function_definition(fun_name, [x, u, z, p], \
                [con_h_expr, jac_ux_t, jac_z_t], constraints_dir)

        if opts['generate_hess']:
            if stage_type == 'terminal':
                fun_name = model.name + '_constr_h_e_fun_jac_uxt_zt_hess'
            elif stage_type == 'initial':
                fun_name = model.name + '_constr_h_0_fun_jac_uxt_zt_hess'
            else:
                fun_name = model.name + '_constr_h_fun_jac_uxt_zt_hess'

            # adjoint
            adj_ux = ca.jtimes(con_h_expr, ca.vertcat(u, x), lam_h, True)
            # hessian
            hess_ux = ca.jacobian(adj_ux, ca.vertcat(u, x), {"symmetric": is_casadi_SX(x)})

            adj_z = ca.jtimes(con_h_expr, z, lam_h, True)
            hess_z = ca.jacobian(adj_z, z, {"symmetric": is_casadi_SX(x)})

            context.add_function_definition(fun_name, [x, u, lam_h, z, p], \
                    [con_h_expr, jac_ux_t, hess_ux, jac_z_t, hess_z], constraints_dir)

        if stage_type == 'terminal':
            fun_name = model.name + '_constr_h_e_fun'
        elif stage_type == 'initial':
            fun_name = model.name + '_constr_h_0_fun'
        else:
            fun_name = model.name + '_constr_h_fun'
        context.add_function_definition(fun_name, [x, u, z, p], [con_h_expr], constraints_dir)

        if opts["with_solution_sens_wrt_params"]:
            jac_p = ca.jacobian(con_h_expr, model.p_global)
            adj_ux = ca.jtimes(con_h_expr, ca.vertcat(u, x), lam_h, True)
            hess_xu_p = ca.jacobian(adj_ux, model.p_global)

            if stage_type == 'terminal':
                fun_name = model.name + '_constr_h_e_jac_p_hess_xu_p'
            elif stage_type == 'initial':
                fun_name = model.name + '_constr_h_0_jac_p_hess_xu_p'
            else:
                fun_name = model.name + '_constr_h_jac_p_hess_xu_p'

            context.add_function_definition(fun_name, [x, u, lam_h, z, p], \
                    [jac_p, hess_xu_p], constraints_dir)

        if opts["with_value_sens_wrt_params"]:
            adj_p = ca.jtimes(con_h_expr, model.p_global, lam_h, True)
            if stage_type == 'terminal':
                fun_name = model.name + '_constr_h_e_adj_p'
            elif stage_type == 'initial':
                fun_name = model.name + '_constr_h_0_adj_p'
            else:
                fun_name = model.name + '_constr_h_adj_p'

            context.add_function_definition(fun_name, [x, u, lam_h, p], [adj_p], constraints_dir)

    else: # BGP constraint
        if stage_type == 'terminal':
            fun_name_prefix = model.name + '_phi_e_constraint'
            r = model.con_r_in_phi_e
            con_r_expr = model.con_r_expr_e
        elif stage_type == 'initial':
            fun_name_prefix = model.name + '_phi_0_constraint'
            r = model.con_r_in_phi_0
            con_r_expr = model.con_r_expr_0
        elif stage_type == 'path':
            fun_name_prefix = model.name + '_phi_constraint'
            r = model.con_r_in_phi
            con_r_expr = model.con_r_expr

        nphi = casadi_length(con_phi_expr)
        con_phi_expr_x_u_z = ca.substitute(con_phi_expr, r, con_r_expr)
        phi_jac_u = ca.jacobian(con_phi_expr_x_u_z, u)
        phi_jac_x = ca.jacobian(con_phi_expr_x_u_z, x)
        phi_jac_z = ca.jacobian(con_phi_expr_x_u_z, z)

        hess = ca.vertcat(*[ca.hessian(con_phi_expr[i], r)[0] for i in range(nphi)])
        hess = ca.substitute(hess, r, con_r_expr)

        r_jac_u = ca.jacobian(con_r_expr, u)
        r_jac_x = ca.jacobian(con_r_expr, x)

        fun_jac_hess_name = fun_name_prefix + '_fun_jac_hess'
        context.add_function_definition(fun_jac_hess_name, [x, u, z, p], \
                [con_phi_expr_x_u_z, \
                ca.vertcat(ca.transpose(phi_jac_u), ca.transpose(phi_jac_x)), \
                ca.transpose(phi_jac_z), \
                hess,
                ca.vertcat(ca.transpose(r_jac_u), ca.transpose(r_jac_x))],
                constraints_dir
                )

        fun_name = fun_name_prefix + '_fun'
        context.add_function_definition(fun_name, [x, u, z, p], [con_phi_expr_x_u_z], constraints_dir)

    return

