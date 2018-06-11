# -*- coding: utf-8 -*-
"""Decomposition solver for Generalized Disjunctive Programming (GDP) problems.

The GDPopt (Generalized Disjunctive Programming optimizer) solver applies a
variety of decomposition-based approaches to solve Generalized Disjunctive
Programming (GDP) problems. GDP models can include nonlinear, continuous
variables and constraints, as well as logical conditions.

These approaches include:

- Outer approximation
- Partial surrogate cuts [pending]
- Generalized Bender decomposition [pending]

This solver implementation was developed by Carnegie Mellon University in the
research group of Ignacio Grossmann.

For nonconvex problems, the bounds self.LB and self.UB may not be rigorous.

Questions: Please make a post at StackOverflow and/or contact Qi Chen
<https://github.com/qtothec>.

"""
from __future__ import division

import logging
from math import fabs

import pyomo.common.plugin
from pyomo.common.config import (ConfigBlock, ConfigList, ConfigValue, In,
                                 NonNegativeFloat, NonNegativeInt)
from pyomo.contrib.gdpopt.cut_generation import (add_integer_cut,
                                                 add_outer_approximation_cuts)
from pyomo.contrib.gdpopt.master_initialize import (init_custom_disjuncts,
                                                    init_max_binaries,
                                                    init_set_covering)
from pyomo.contrib.gdpopt.loa import solve_OA_master
from pyomo.contrib.gdpopt.nlp_solve import (solve_NLP,
                                            update_nlp_progress_indicators)
from pyomo.contrib.gdpopt.util import (GDPoptSolveData,
                                       build_ordered_component_lists,
                                       _DoNothing, _record_problem_statistics,
                                       a_logger, copy_var_list_values,
                                       reformulate_integer_variables,
                                       clone_orig_model_with_lists,
                                       validate_disjunctions,
                                       algorithm_should_terminate)
from pyomo.core.base import (Block, Constraint, ConstraintList,
                             Objective, Reals, Suffix, TransformationFactory,
                             Var, minimize, value)
from pyomo.core.kernel import ComponentMap
from pyomo.opt.base import IOptSolver
from pyomo.opt.results import ProblemSense

__version__ = (0, 3, 0)


class GDPoptSolver(pyomo.common.plugin.Plugin):
    """A decomposition-based GDP solver."""

    pyomo.common.plugin.implements(IOptSolver)
    pyomo.common.plugin.alias(
        'gdpopt',
        doc='The GDPopt decomposition-based '
        'Generalized Disjunctive Programming (GDP) solver')

    CONFIG = ConfigBlock("GDPopt")
    CONFIG.declare("iterlim", ConfigValue(
        default=30, domain=NonNegativeInt,
        description="Iteration limit."
    ))
    CONFIG.declare("strategy", ConfigValue(
        default="LOA", domain=In(["LOA"]),
        description="Decomposition strategy to use."
    ))
    CONFIG.declare("init_strategy", ConfigValue(
        default="set_covering", domain=In([
            "set_covering", "max_binary", "fixed_binary", "custom_disjuncts"]),
        description="Initialization strategy to use.",
        doc="""Selects the initialization strategy to use when generating
        the initial cuts to construct the master problem."""
    ))
    CONFIG.declare("custom_init_disjuncts", ConfigList(
        # domain=ComponentSets of Disjuncts,
        default=None,
        description="List of disjunct sets to use for initialization."
    ))
    CONFIG.declare("max_slack", ConfigValue(
        default=1000, domain=NonNegativeFloat,
        description="Upper bound on slack variables for OA"
    ))
    CONFIG.declare("OA_penalty_factor", ConfigValue(
        default=1000, domain=NonNegativeFloat,
        description="Penalty multiplication term for slack variables on the "
        "objective value."
    ))
    CONFIG.declare("set_cover_iterlim", ConfigValue(
        default=8, domain=NonNegativeInt,
        description="Limit on the number of set covering iterations."
    ))
    CONFIG.declare("mip", ConfigValue(
        default="gurobi",
        description="Mixed integer linear solver to use."
    ))
    mip_options = CONFIG.declare("mip_options", ConfigBlock(implicit=True))
    CONFIG.declare("nlp", ConfigValue(
        default="ipopt",
        description="Nonlinear solver to use"))
    nlp_options = CONFIG.declare("nlp_options", ConfigBlock(implicit=True))
    CONFIG.declare("master_postsolve", ConfigValue(
        default=_DoNothing,
        description="callback hook after a solution of the master problem"
    ))
    CONFIG.declare("subprob_presolve", ConfigValue(
        default=_DoNothing,
        description="callback hook before calling the subproblem solver"
    ))
    CONFIG.declare("subprob_postsolve", ConfigValue(
        default=_DoNothing,
        description="callback hook after a solution of the "
        "nonlinear subproblem"
    ))
    CONFIG.declare("subprob_postfeas", ConfigValue(
        default=_DoNothing,
        description="callback hook after feasible solution of "
        "the nonlinear subproblem"
    ))
    CONFIG.declare("algorithm_stall_after", ConfigValue(
        default=2,
        description="number of non-improving master iterations after which "
        "the algorithm will stall and exit."
    ))
    CONFIG.declare("tee", ConfigValue(
        default=False,
        description="Stream output to terminal.",
        domain=bool
    ))
    CONFIG.declare("logger", ConfigValue(
        default='pyomo.contrib.gdpopt',
        description="The logger object or name to use for reporting.",
        domain=a_logger
    ))
    CONFIG.declare("bound_tolerance", ConfigValue(
        default=1E-6, domain=NonNegativeFloat,
        description="Tolerance for bound convergence."
    ))
    CONFIG.declare("small_dual_tolerance", ConfigValue(
        default=1E-8,
        description="When generating cuts, small duals multiplied "
        "by expressions can cause problems. Exclude all duals "
        "smaller in absolue value than the following."
    ))
    CONFIG.declare("integer_tolerance", ConfigValue(
        default=1E-5,
        description="Tolerance on integral values."
    ))
    CONFIG.declare("constraint_tolerance", ConfigValue(
        default=1E-6,
        description="Tolerance on constraint satisfaction."
    ))
    CONFIG.declare("variable_tolerance", ConfigValue(
        default=1E-8,
        description="Tolerance on variable bounds."
    ))
    CONFIG.declare("round_NLP_binaries", ConfigValue(
        default=True,
        description="flag to round binary values to exactly 0 or 1. "
        "Rounding is done before fixing disjuncts."
    ))
    CONFIG.declare("reformulate_integer_vars_using", ConfigValue(
        default=None,
        description="The method to use for reformulating integer variables "
        "into binary for this solver."
    ))

    def available(self, exception_flag=True):
        """Check if solver is available.

        TODO: For now, it is always available. However, sub-solvers may not
        always be available, and so this should reflect that possibility.

        """
        return True

    def version(self):
        """Return a 3-tuple describing the solver version."""
        return __version__

    def solve(self, model, **kwds):
        """Solve the model.

        Warning: this solver is still in beta. Keyword arguments subject to
        change. Undocumented keyword arguments definitely subject to change.

        This function performs all of the GDPopt solver setup and problem
        validation. It then calls upon helper functions to construct the
        initial master approximation and iteration loop.

        Args:
            model (Block): a Pyomo model or block to be solved

        """
        config = self.CONFIG(kwds.pop('options', {}))
        config.set_value(kwds)
        solve_data = GDPoptSolveData()

        old_logger_level = config.logger.getEffectiveLevel()
        try:
            if config.tee and old_logger_level > logging.INFO:
                # If the logger does not already include INFO, include it.
                config.logger.setLevel(logging.INFO)
            config.logger.info("---Starting GDPopt---")

            # Create a model block on which to store GDPopt-specific utility
            # modeling objects.
            if hasattr(model, 'GDPopt_utils'):
                raise RuntimeError(
                    "GDPopt needs to create a Block named GDPopt_utils "
                    "on the model object, but an attribute with that name "
                    "already exists.")
            else:
                model.GDPopt_utils = Block(
                    doc="Container for GDPopt solver utility modeling objects")

            solve_data.original_model = model

            solve_data.working_model = m = clone_orig_model_with_lists(model)
            GDPopt = solve_data.working_model.GDPopt_utils

            solve_data.current_strategy = config.strategy

            # Reformulate integer variables to binary
            reformulate_integer_variables(model, config)

            # Save ordered lists of main modeling components, so that data can
            # be easily transferred between future model clones.
            build_ordered_component_lists(solve_data.working_model)
            _record_problem_statistics(m, solve_data, config)
            solve_data.results.solver.name = 'GDPopt ' + str(self.version())

            # Save model initial values. These are used later to initialize NLP
            # subproblems.
            GDPopt.initial_var_values = list(
                v.value for v in GDPopt.working_var_list)

            # Store the initial model state as the best solution found. If we
            # find no better solution, then we will restore from this copy.
            solve_data.best_solution_found = list(GDPopt.initial_var_values)

            # Validate the model to ensure that GDPopt is able to solve it.
            self._validate_model(config, solve_data)

            # Maps in order to keep track of certain generated constraints
            GDPopt.oa_cut_map = ComponentMap()

            # Integer cuts exclude particular discrete decisions
            GDPopt.integer_cuts = ConstraintList(doc='integer cuts')

            # Feasible integer cuts exclude discrete realizations that have
            # been explored via an NLP subproblem. Depending on model
            # characteristics, the user may wish to revisit NLP subproblems
            # (with a different initialization, for example). Therefore, these
            # cuts are not enabled by default, unless the initial model has no
            # discrete decisions.

            # Note: these cuts will only exclude integer realizations that are
            # not already in the primary GDPopt_integer_cuts ConstraintList.
            GDPopt.no_backtracking = ConstraintList(
                doc='explored integer cuts')
            if not solve_data.no_discrete_decisions:
                # If there are multiple discrete decisions, allow re-visiting
                # them by default. Otherwise, no point in resolving the
                # problem.
                GDPopt.no_backtracking.deactivate()

            # Set up iteration counters
            solve_data.master_iteration = 0
            solve_data.mip_iteration = 0
            solve_data.nlp_iteration = 0

            # set up bounds
            solve_data.LB = float('-inf')
            solve_data.UB = float('inf')
            solve_data.iteration_log = {}

            # Flag indicating whether the solution improved in the past
            # iteration or not
            solve_data.feasible_solution_improved = False

            # Initialize the master problem
            self._GDPopt_initialize_master(solve_data, config)

            # Algorithm main loop
            self._GDPopt_iteration_loop(solve_data, config)

            # Update values in working model
            copy_var_list_values(
                from_list=solve_data.best_solution_found,
                to_list=GDPopt.working_var_list,
                config=config)
            GDPopt.objective_value.set_value(
                value(solve_data.working_objective_expr))

            # Update values in original model
            copy_var_list_values(
                GDPopt.orig_var_list,
                solve_data.original_model.GDPopt_utils.orig_var_list,
                config)

            solve_data.results.problem.lower_bound = solve_data.LB
            solve_data.results.problem.upper_bound = solve_data.UB

        finally:
            config.logger.setLevel(old_logger_level)

    def _validate_model(self, config, solve_data):
        """Validate that the model is solveable by GDPopt.

        Also populates results object with problem information.

        """
        m = solve_data.working_model
        GDPopt = m.GDPopt_utils

        # Check for any integer variables
        if solve_data.results.problem.number_of_integer_variables > 0:
            raise ValueError('Model contains unfixed integer variables. '
                             'GDPopt does not currently support solution of '
                             'such problems.')
            # TODO add in the reformulation using base 2

        # Handle LP/NLP being passed to the solver
        prob = solve_data.results.problem
        if (prob.number_of_binary_variables == 0 and
                prob.number_of_disjunctions == 0):
            config.logger.info('Problem has no discrete decisions.')
            solve_data.no_discrete_decisions = True
        else:
            solve_data.no_discrete_decisions = False

        # Handle missing or multiple objectives
        objs = list(m.component_data_objects(
            ctype=Objective, active=True, descend_into=True))
        num_objs = len(objs)
        solve_data.results.problem.number_of_objectives = num_objs
        if num_objs == 0:
            config.logger.warning(
                'Model has no active objectives. Adding dummy objective.')
            GDPopt.dummy_objective = Objective(expr=1)
            main_obj = GDPopt.dummy_objective
        elif num_objs > 1:
            raise ValueError('Model has multiple active objectives.')
        else:
            main_obj = objs[0]
        solve_data.working_objective_expr = main_obj.expr

        # Move the objective to the constraints

        # TODO only move the objective if nonlinear?
        GDPopt.objective_value = Var(domain=Reals, initialize=0)
        solve_data.objective_sense = main_obj.sense
        if main_obj.sense == minimize:
            GDPopt.objective_expr = Constraint(
                expr=GDPopt.objective_value >= main_obj.expr)
            solve_data.results.problem.sense = ProblemSense.minimize
        else:
            GDPopt.objective_expr = Constraint(
                expr=GDPopt.objective_value <= main_obj.expr)
            solve_data.results.problem.sense = ProblemSense.maximize
        main_obj.deactivate()
        GDPopt.objective = Objective(
            expr=GDPopt.objective_value, sense=main_obj.sense)

        # TODO if any continuous variables are multipled with binary ones, need
        # to do some kind of transformation (Glover?) or throw an error message

    def _GDPopt_initialize_master(self, solve_data, config):
        """Initialize the decomposition algorithm.

        This includes generating the initial cuts require to build the master
        problem.

        """
        config.logger.info("---Starting GDPopt initialization---")
        m = solve_data.working_model
        if not hasattr(m, 'dual'):  # Set up dual value reporting
            m.dual = Suffix(direction=Suffix.IMPORT)
        m.dual.activate()

        solve_data.linear_GDP = m.clone()
        # deactivate nonlinear constraints
        for c in solve_data.linear_GDP.GDPopt_utils.\
                working_nonlinear_constraints:
            c.deactivate()

        # Initialization strategies
        valid_init_strategies = {
            'set_covering': init_set_covering,
            'max_binary': init_max_binaries,
            'fixed_binary': None,
            'custom_disjuncts': init_custom_disjuncts
        }
        init_strategy = valid_init_strategies.get(config.init_strategy, None)
        if init_strategy is not None:
            init_strategy(solve_data, config)
        else:
            raise ValueError(
                'Unknown initialization strategy: %s. '
                'Valid strategies include: %s'
                % (config.init_strategy,
                   ", ".join(valid_init_strategies.keys())))

    def _GDPopt_iteration_loop(self, solve_data, config):
        """Algorithm main loop.

        Returns True if successful convergence is obtained. False otherwise.

        """
        while solve_data.master_iteration < config.iterlim:
            # print line for visual display
            solve_data.master_iteration += 1
            solve_data.mip_iteration = 0
            solve_data.nlp_iteration = 0
            config.logger.info(
                '---GDPopt Master Iteration %s---'
                % solve_data.master_iteration)
            # solve MILP master problem
            if solve_data.current_strategy == 'LOA':
                mip_results = solve_OA_master(solve_data, config)
                if mip_results:
                    _, mip_var_values = mip_results
            # Check termination conditions
            if algorithm_should_terminate(solve_data, config):
                break
            # Solve NLP subproblem
            nlp_model = solve_data.working_model.clone()
            solve_data.nlp_iteration += 1
            # copy in the discrete variable values
            for var, val in zip(nlp_model.GDPopt_utils.working_var_list,
                                mip_var_values):
                if val is None:
                    continue
                if not var.is_binary():
                    var.value = val
                elif ((fabs(val) <= config.integer_tolerance or
                       fabs(val - 1) <= config.integer_tolerance)
                      and config.round_NLP_binaries):
                    # Round the binary variables to 0 or 1 if appropriate.
                    var.value = int(round(val))
                else:
                    raise ValueError(
                        "Binary variable %s value %s is not "
                        "within tolerance %s of 0 or 1." %
                        (var.name, var.value, config.integer_tolerance))
            TransformationFactory('gdp.fix_disjuncts').apply_to(nlp_model)
            for var in nlp_model.GDPopt_utils.working_var_list:
                if var.is_binary():
                    var.fix()
            nlp_result = solve_NLP(nlp_model, solve_data, config)
            nlp_feasible, nlp_var_values, nlp_duals = nlp_result
            if nlp_feasible:
                update_nlp_progress_indicators(nlp_model, solve_data, config)
                add_outer_approximation_cuts(
                    nlp_var_values, nlp_duals, solve_data, config)
            add_integer_cut(
                mip_var_values, solve_data, config, feasible=nlp_feasible)

            # Check termination conditions
            if algorithm_should_terminate(solve_data, config):
                break
