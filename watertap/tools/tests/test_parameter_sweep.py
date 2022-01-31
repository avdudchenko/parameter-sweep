###############################################################################
# WaterTAP Copyright (c) 2021, The Regents of the University of California,
# through Lawrence Berkeley National Laboratory, Oak Ridge National
# Laboratory, National Renewable Energy Laboratory, and National Energy
# Technology Laboratory (subject to receipt of any required approvals from
# the U.S. Dept. of Energy). All rights reserved.
#
# Please see the files COPYRIGHT.md and LICENSE.md for full copyright and license
# information, respectively. These files are also available online at the URL
# "https://github.com/watertap-org/watertap/"
#
###############################################################################

import pytest
import os
import numpy as np
import pyomo.environ as pyo

from pyomo.environ import value

from watertap.tools.parameter_sweep import (_init_mpi,
                                               _build_combinations,
                                               _divide_combinations,
                                               _update_model_values,
                                               _aggregate_results,
                                               _interp_nan_values,
                                               _process_sweep_params,
                                               _write_output_to_h5,
                                               _read_output_h5,
                                               _create_local_output_skeleton,
                                               _create_global_output,
                                               parameter_sweep,
                                               LinearSample,
                                               UniformSample,
                                               NormalSample,
                                               SamplingType)

# -----------------------------------------------------------------------------

class TestParallelManager():
    @pytest.fixture(scope="class")
    def model(self):
        m = pyo.ConcreteModel()
        m.fs = fs = pyo.Block()

        fs.input = pyo.Var(['a','b'], within=pyo.UnitInterval, initialize=0.5)
        fs.output = pyo.Var(['c', 'd'], within=pyo.UnitInterval, initialize=0.5)

        fs.slack = pyo.Var(['ab_slack', 'cd_slack'], bounds=(0,0), initialize=0.0)
        fs.slack_penalty = pyo.Param(default=1000., mutable=True, within=pyo.PositiveReals)

        fs.ab_constr = pyo.Constraint(expr=(fs.output['c'] + fs.slack['ab_slack'] == 2*fs.input['a']))
        fs.cd_constr = pyo.Constraint(expr=(fs.output['d'] + fs.slack['cd_slack'] == 3*fs.input['b']))

        fs.performance = pyo.Expression(expr=pyo.summation(fs.output))

        m.objective = pyo.Objective(expr=m.fs.performance - m.fs.slack_penalty*pyo.summation(m.fs.slack),
                                    sense=pyo.maximize)
        return m

    @pytest.mark.unit
    def test_init_mpi(self):
        comm, rank, num_procs = _init_mpi()

        assert type(rank) == int
        assert type(num_procs) == int
        assert 0 <= rank < num_procs

    @pytest.mark.unit
    def test_single_index_unrolled(self):
        indexed_var = pyo.Var(['a'])
        indexed_var.construct()

        ls = LinearSample(indexed_var, None, None, None)

        assert ls.pyomo_object is indexed_var['a']

    @pytest.mark.unit
    def test_multiple_indices_error(self):
        indexed_var = pyo.Var(['a', 'b'])
        indexed_var.construct()

        with pytest.raises(Exception):
            ls = LinearSample(indexed_var, None, None, None)

    @pytest.mark.component
    def test_linear_build_combinations(self):
        comm, rank, num_procs = _init_mpi()

        A_param = pyo.Param(initialize=0.0, mutable=True)
        B_param = pyo.Param(initialize=1.0, mutable=True)
        C_param = pyo.Param(initialize=2.0, mutable=True)

        range_A = [0.0, 10.0]
        range_B = [1.0, 20.0]
        range_C = [2.0, 30.0]

        nn_A = 4
        nn_B = 5
        nn_C = 6

        param_dict = dict()
        param_dict['var_A'] = LinearSample(A_param, range_A[0], range_A[1], nn_A)
        param_dict['var_B']  = LinearSample(B_param, range_B[0], range_B[1], nn_B)
        param_dict['var_C']  = LinearSample(C_param, range_C[0], range_C[1], nn_C)

        global_combo_array = _build_combinations(param_dict, SamplingType.FIXED, None, comm, rank, num_procs)

        assert np.shape(global_combo_array)[0] == nn_A*nn_B*nn_C
        assert np.shape(global_combo_array)[1] == len(param_dict)

        assert global_combo_array[0, 0] == pytest.approx(range_A[0])
        assert global_combo_array[0, 1] == pytest.approx(range_B[0])
        assert global_combo_array[0, 2] == pytest.approx(range_C[0])

        assert global_combo_array[-1, 0] == pytest.approx(range_A[1])
        assert global_combo_array[-1, 1] == pytest.approx(range_B[1])
        assert global_combo_array[-1, 2] == pytest.approx(range_C[1])

    def test_random_build_combinations(self):
        comm, rank, num_procs = _init_mpi()

        nn = int(1e5)

        # Uniform random sampling [lower_limit, upper_limit]
        A_param = pyo.Param(initialize=-10.0, mutable=True)
        B_param = pyo.Param(initialize=0.0, mutable=True)
        C_param = pyo.Param(initialize=10.0, mutable=True)

        range_A = [-10.0, 0.0]
        range_B = [0.0, 10.0]
        range_C = [10.0, 20.0]

        param_dict = dict()
        param_dict['var_A'] = UniformSample(A_param, range_A[0], range_A[1])
        param_dict['var_B']  = UniformSample(B_param, range_B[0], range_B[1])
        param_dict['var_C']  = UniformSample(C_param, range_C[0], range_C[1])

        global_combo_array = _build_combinations(param_dict, SamplingType.RANDOM, nn, comm, rank, num_procs)

        assert np.shape(global_combo_array)[0] == nn
        assert np.shape(global_combo_array)[1] == len(param_dict)

        assert np.all(range_A[0] < global_combo_array[:, 0])
        assert np.all(range_B[0] < global_combo_array[:, 1])
        assert np.all(range_C[0] < global_combo_array[:, 2])

        assert np.all(global_combo_array[:, 0] < range_A[1])
        assert np.all(global_combo_array[:, 1] < range_B[1])
        assert np.all(global_combo_array[:, 2] < range_C[1])

        # Normal random sampling [mean, stdev]
        A_param = pyo.Param(initialize=10.0, mutable=True)
        B_param = pyo.Param(initialize=100.0, mutable=True)
        C_param = pyo.Param(initialize=1000.0, mutable=True)

        range_A = [10.0, 5.0]
        range_B = [100.0, 50.0]
        range_C = [1000.0, 0.0]

        param_dict = dict()
        param_dict['var_A'] = NormalSample(A_param, range_A[0], range_A[1])
        param_dict['var_B']  = NormalSample(B_param, range_B[0], range_B[1])
        param_dict['var_C']  = NormalSample(C_param, range_C[0], range_C[1])

        global_combo_array = _build_combinations(param_dict, SamplingType.RANDOM, nn, comm, rank, num_procs)

        assert np.shape(global_combo_array)[0] == nn
        assert np.shape(global_combo_array)[1] == len(param_dict)

        assert np.mean(global_combo_array[:, 0]) < (range_A[0] + range_A[1])
        assert np.mean(global_combo_array[:, 1]) < (range_B[0] + range_B[1])

        assert (range_A[0] - range_A[1]) < np.mean(global_combo_array[:, 0])
        assert (range_B[0] - range_B[1]) < np.mean(global_combo_array[:, 1])

        assert np.all(global_combo_array[:, 2] == range_C[0])

    @pytest.mark.component
    def test_divide_combinations(self):
        # _divide_combinations(global_combo_array, rank, num_procs)

        comm, rank, num_procs = _init_mpi()

        A_param = pyo.Param(initialize=0.0, mutable=True)
        B_param = pyo.Param(initialize=1.0, mutable=True)
        C_param = pyo.Param(initialize=2.0, mutable=True)

        range_A = [0.0, 10.0]
        range_B = [1.0, 20.0]
        range_C = [2.0, 30.0]

        nn_A = 4
        nn_B = 5
        nn_C = 6

        param_dict = dict()
        param_dict['var_A'] = LinearSample(A_param, range_A[0], range_A[1], nn_A)
        param_dict['var_B']  = LinearSample(B_param, range_B[0], range_B[1], nn_B)
        param_dict['var_C']  = LinearSample(C_param, range_C[0], range_C[1], nn_C)

        global_combo_array = _build_combinations(param_dict, SamplingType.FIXED, None, comm, rank, num_procs)

        test = np.array_split(global_combo_array, num_procs, axis=0)[rank]

        local_combo_array = _divide_combinations(global_combo_array, rank, num_procs)

        assert np.shape(local_combo_array)[1] == 3

        assert np.allclose(test[:, 0], local_combo_array[:, 0])
        assert np.allclose(test[:, 1], local_combo_array[:, 1])
        assert np.allclose(test[:, 2], local_combo_array[:, 2])

        if rank == 0:
            assert local_combo_array[0, 0] == pytest.approx(range_A[0])
            assert local_combo_array[0, 1] == pytest.approx(range_B[0])
            assert local_combo_array[0, 2] == pytest.approx(range_C[0])

        if rank == num_procs - 1:
            assert local_combo_array[-1, 0] == pytest.approx(range_A[1])
            assert local_combo_array[-1, 1] == pytest.approx(range_B[1])
            assert local_combo_array[-1, 2] == pytest.approx(range_C[1])

    @pytest.mark.component
    def test_update_model_values(self, model):
        m = model

        param_dict = dict()
        param_dict['input_a'] = LinearSample(m.fs.input['a'], None, None, None)
        param_dict['input_b'] = LinearSample(m.fs.input['b'], None, None, None)

        original_a = value(m.fs.input['a'])
        original_b = value(m.fs.input['b'])

        new_values = [1.1*original_a, 1.1*original_b]

        _update_model_values(m, param_dict, new_values)

        assert value(m.fs.input['a']) == pytest.approx(new_values[0])
        assert value(m.fs.input['b']) == pytest.approx(new_values[1])

    @pytest.mark.unit
    def test_aggregate_results(self):
        comm, rank, num_procs = _init_mpi()

        # print('Rank %d, num_procs %d' % (rank, num_procs))

        nn = 5
        np.random.seed(1)
        local_results = (rank+1)*np.random.rand(nn, 2)
        global_values = np.random.rand(nn*num_procs, 4)

        global_results = _aggregate_results(local_results, global_values, comm, num_procs)

        assert np.shape(global_results)[1] == np.shape(local_results)[1]
        assert np.shape(global_results)[0] == np.shape(global_values)[0]

        if rank == 0:
            assert global_results[0, 0] == pytest.approx(local_results[0, 0])
            assert global_results[0, 1] == pytest.approx(local_results[0, 1])
            assert global_results[-1, 0] == pytest.approx(num_procs*local_results[-1, 0])
            assert global_results[-1, 1] == pytest.approx(num_procs*local_results[-1, 1])

    @pytest.mark.unit
    def test_interp_nan_values(self):

        global_values = np.array([[  0,   0,   0],
                                  [  0,   0,   1],
                                  [  0,   1,   0],
                                  [  0,   1,   1],
                                  [  1,   0,   0],
                                  [  1,   0,   1],
                                  [  1,   1,   0],
                                  [  1,   1,   1],
                                  [0.5, 0.5, 0.5],
                                  [  1,   1,   1]])

        global_results = np.array([0, 1, 2, 3, 4, 5, 6, 7, np.nan, np.nan])[np.newaxis].T

        global_results_clean = _interp_nan_values(global_values, global_results)

        assert np.shape(global_results_clean)[1] == np.shape(global_results)[1]
        assert np.shape(global_results_clean)[0] == np.shape(global_results)[0]

        assert(global_results_clean[8]) == pytest.approx(np.mean(global_results[0:8]))
        assert(global_results_clean[9]) == pytest.approx(global_results[7])

    @pytest.mark.unit
    def test_h5_read_write(self, tmp_path):
        comm, rank, num_procs = _init_mpi()
        tmp_path = _get_rank0_path(comm, tmp_path)

        reference_dict = {'outputs': {'fs.input[a]': {'lower bound': 0,
                                                      'units': 'non-dimensional',
                                                      'upper bound': 0,
                                                      'value': np.array([0., 0., 0., 0., 0., 0., 0., 0., 0.])},
                                      'fs.input[b]': {'lower bound': 0,
                                                      'units': 'non-dimensional',
                                                      'upper bound': 0,
                                                      'value': np.array([0., 0., 0., 0., 0., 0., 0., 0., 0.])},
                                      'fs.output[c]': {'lower bound': 0,
                                                       'units': 'non-dimensional',
                                                       'upper bound': 0,
                                                       'value': np.array([0.2, 0.2, 0. , 1. , 1. , 0. , 0. , 0. , 0. ])},
                                      'fs.output[d]': {'lower bound': 0,
                                                       'units': 'non-dimensional',
                                                       'upper bound': 0,
                                                       'value': np.array([0.  , 0.75, 0.  , 0.  , 0.75, 0.  , 0.  , 0.  , 0.  ])},
                                      'fs.slack[ab_slack]': {'lower bound': 0,
                                                             'units': 'non-dimensional',
                                                             'upper bound': 0,
                                                             'value': np.array([0., 0., 0., 0., 0., 0., 0., 0., 0.])},
                                      'fs.slack[cd_slack]': {'lower bound': 0,
                                                            'units': 'non-dimensional',
                                                            'upper bound': 0,
                                                            'value': np.array([0., 0., 0., 0., 0., 0., 0., 0., 0.])}},
                         'solve_status': ['optimal',
                                          'optimal',
                                          'optimal',
                                          'optimal',
                                          'optimal',
                                          'optimal',
                                          'optimal',
                                          'optimal',
                                          'optimal'],
                         'sweep_params': {'fs.input[a]': {'lower bound': 0,
                                                          'units': 'non-dimensional',
                                                          'upper bound': 0,
                                                          'value': np.array([0.1, 0.1, 0. , 0.5, 0.5, 0. , 0. , 0. , 0. ])},
                                          'fs.input[b]': {'lower bound': 0,
                                                          'units': 'non-dimensional',
                                                          'upper bound': 0,
                                                          'value': np.array([0.  , 0.25, 0.  , 0.  , 0.25, 0.  , 0.  , 0.  , 0.  ])}}}

        h5_fname = "h5_test_{0}.h5".format(rank)
        _write_output_to_h5(reference_dict, output_directory=tmp_path, fname=h5_fname)
        read_dictionary = _read_output_h5(os.path.join(tmp_path, h5_fname))
        _assert_dictionary_correctness(reference_dict, read_dictionary)

    @pytest.mark.unit
    def test_create_local_output_skeleton(self, model):
        comm, rank, num_procs = _init_mpi()

        m = model
        m.fs.slack_penalty = 1000.
        m.fs.slack.setub(0)

        sweep_params = {'input_a' : (m.fs.input['a'], 0.1, 0.9, 3),
                        'input_b' : (m.fs.input['b'], 0.0, 0.5, 3)}
        outputs = {'output_c':m.fs.output['c'],
                   'output_d':m.fs.output['d'],
                   'performance':m.fs.performance}

        sweep_params, sampling_type = _process_sweep_params(sweep_params)
        values = _build_combinations(sweep_params, sampling_type, None, comm, rank, num_procs)
        num_cases = np.shape(values)[0]
        output_dict = _create_local_output_skeleton(model, sweep_params, num_cases)

        truth_dict = {'outputs': {'fs.input[a]': {'lower bound': 0,
                                                  'units': 'non-dimensional',
                                                  'upper bound': 0,
                                                  'value': np.array([0., 0., 0., 0., 0., 0., 0., 0., 0.])},
                                  'fs.input[b]': {'lower bound': 0,
                                                  'units': 'non-dimensional',
                                                  'upper bound': 0,
                                                  'value': np.array([0., 0., 0., 0., 0., 0., 0., 0., 0.])},
                                  'fs.output[c]': {'lower bound': 0,
                                                   'units': 'non-dimensional',
                                                   'upper bound': 0,
                                                   'value': np.array([0., 0., 0., 0., 0., 0., 0., 0., 0.])},
                                  'fs.output[d]': {'lower bound': 0,
                                                   'units': 'non-dimensional',
                                                   'upper bound': 0,
                                                   'value': np.array([0., 0., 0., 0., 0., 0., 0., 0., 0.])},
                                  'fs.performance': {'lower bound': 0,
                                                     'units': 'non-dimensional',
                                                     'upper bound': 0,
                                                     'value': np.array([0., 0., 0., 0., 0., 0., 0., 0., 0.])},
                                  'fs.slack[ab_slack]': {'lower bound': 0,
                                                         'units': 'non-dimensional',
                                                         'upper bound': 0,
                                                         'value': np.array([0., 0., 0., 0., 0., 0., 0., 0., 0.])},
                                  'fs.slack[cd_slack]': {'lower bound': 0,
                                                         'units': 'non-dimensional',
                                                         'upper bound': 0,
                                                         'value': np.array([0., 0., 0., 0., 0., 0., 0., 0., 0.])},
                                  'objective': {'lower bound': 0,
                                                'units': 'non-dimensional',
                                                'upper bound': 0,
                                                'value': np.array([0., 0., 0., 0., 0., 0., 0., 0., 0.])}},
                      'sweep_params': {'fs.input[a]': {'lower bound': 0,
                                                       'units': 'non-dimensional',
                                                       'upper bound': 0,
                                                       'value': np.array([0., 0., 0., 0., 0., 0., 0., 0., 0.])},
                                       'fs.input[b]': {'lower bound': 0,
                                                       'units': 'non-dimensional',
                                                       'upper bound': 0,
                                                       'value': np.array([0., 0., 0., 0., 0., 0., 0., 0., 0.])}}}

        _assert_dictionary_correctness(truth_dict, output_dict)

    @pytest.mark.unit
    def test_create_global_output(self, model):
        comm, rank, num_procs = _init_mpi()

        m = model
        m.fs.slack_penalty = 1000.
        m.fs.slack.setub(0)

        sweep_params = {'input_a' : (m.fs.input['a'], 0.1, 0.9, 3),
                        'input_b' : (m.fs.input['b'], 0.0, 0.5, 3)}
        outputs = {'output_c':m.fs.output['c'],
                   'output_d':m.fs.output['d'],
                   'performance':m.fs.performance}

        sweep_params, sampling_type = _process_sweep_params(sweep_params)
        # Get the globale sweep param values
        global_num_cases = 2*num_procs
        global_values = _build_combinations(sweep_params, sampling_type, global_num_cases, comm, rank, num_procs)
        # divide the workload between processors
        local_values = _divide_combinations(global_values, rank, num_procs)
        local_num_cases = np.shape(local_values)[0]


        local_output_dict = _create_local_output_skeleton(model, sweep_params, local_num_cases)

        # Manually update the values in the numpy array
        for key, value in local_output_dict.items():
            for subkey, subvalue in value.items():
                subvalue['value'][:] = rank+1.0

        # Local output dict also contains the solve_status. The solve status is
        # based on the
        local_output_dict['solve_status'] = ['optimal' for i in range(local_num_cases)]

        # Get the global output dictionary, This is properly created only on rank 0
        global_output_dict = _create_global_output(local_output_dict, global_num_cases, comm)

        if num_procs == 1:
            assert local_output_dict == global_output_dict
        else:
            comm.Barrier()
            if rank > 0:
                assert global_output_dict == local_output_dict
            else:
                test_array = np.repeat(np.arange(1,num_procs+1, dtype=float), 2)
                test_list = ['optimal' for i in range(global_num_cases)]
                for key, value in global_output_dict.items():
                    if key != 'solve_status':
                        for subkey, subvalue in value.items():
                            assert np.allclose(subvalue['value'], test_array)
                    elif key == 'solve_status':
                        assert value == test_list
            comm.Barrier()


    @pytest.mark.component
    def test_parameter_sweep(self, model, tmp_path):
        comm, rank, num_procs = _init_mpi()
        tmp_path = _get_rank0_path(comm, tmp_path)

        m = model
        m.fs.slack_penalty = 1000.
        m.fs.slack.setub(0)

        sweep_params = {'input_a' : (m.fs.input['a'], 0.1, 0.9, 3),
                        'input_b' : (m.fs.input['b'], 0.0, 0.5, 3)}
        outputs = {'output_c':m.fs.output['c'],
                   'output_d':m.fs.output['d'],
                   'performance':m.fs.performance}
        results_file = os.path.join(tmp_path, 'global_results.csv')
        h5_fname = "output_dict"

        # Call the parameter_sweep function
        parameter_sweep(m, sweep_params, outputs,
                csv_results_file = results_file,
                results_fname = h5_fname,
                optimize_function=_optimization,
                debugging_data_dir = tmp_path,
                mpi_comm = comm)

        # NOTE: rank 0 "owns" tmp_path, so it needs to be
        #       responsible for doing any output file checking
        #       tmp_path can be deleted as soon as this method
        #       returns
        if rank == 0:
            # Check that the global results file is created
            assert os.path.isfile(results_file)

            # Check that all local output files have been created
            for k in range(num_procs):
                assert os.path.isfile(os.path.join(tmp_path,f'local_results_{k:03}.csv'))

            # Attempt to read in the data
            data = np.genfromtxt(results_file, skip_header=1, delimiter=',')

            # Compare the last row of the imported data to truth
            truth_data = [ 0.9, 0.5, np.nan, np.nan, np.nan]
            assert np.allclose(data[-1], truth_data, equal_nan=True)

        # Check for the h5 output
        if rank == 0:
            truth_dict = {'outputs': {'fs.input[a]': {'lower bound': 0,
                                                      'units': 'non-dimensional',
                                                      'upper bound': 0,
                                                      'value': np.array([0.1, 0.1, 0.1, 0.5, 0.5, 0.5, 0.9, 0.9, 0.9])},
                                      'fs.input[b]': {'lower bound': 0,
                                                      'units': 'non-dimensional',
                                                      'upper bound': 0,
                                                      'value': np.array([0.  , 0.25, 0.5 , 0.  , 0.25, 0.5 , 0.  , 0.25, 0.5 ])},
                                      'fs.output[c]': {'lower bound': 0,
                                                       'units': 'non-dimensional',
                                                       'upper bound': 0,
                                                       'value': np.array([0.2, 0.2, np.nan, 1., 1., np.nan, np.nan, np.nan, np.nan])},
                                      'fs.output[d]': {'lower bound': 0,
                                                       'units': 'non-dimensional',
                                                       'upper bound': 0,
                                                       'value': np.array([0.  , 0.75,  np.nan, 0., 0.75,  np.nan, np.nan, np.nan, np.nan])},
                                      'fs.performance': {'lower bound': 0,
                                                         'units': 'non-dimensional',
                                                         'upper bound': 0,
                                                         'value': np.array([0.2 , 0.95,  np.nan, 1., 1.75,  np.nan, np.nan,  np.nan, np.nan])},
                                      'fs.slack[ab_slack]': {'lower bound': 0,
                                                             'units': 'non-dimensional',
                                                             'upper bound': 0,
                                                             'value': np.array([ 0.,  0., np.nan,  0.,  0., np.nan, np.nan, np.nan, np.nan])},
                                      'fs.slack[cd_slack]': {'lower bound': 0,
                                                             'units': 'non-dimensional',
                                                             'upper bound': 0,
                                                             'value': np.array([ 0.,  0., np.nan,  0.,  0., np.nan, np.nan, np.nan, np.nan])},
                                      'objective': {'lower bound': 0,
                                                    'units': 'non-dimensional',
                                                    'upper bound': 0,
                                                    'value': np.array([0.2 , 0.95,  np.nan, 1., 1.75, np.nan, np.nan, np.nan, np.nan])}},
                          'solve_status': ['optimal',
                                           'optimal',
                                           'infeasible',
                                           'optimal',
                                           'optimal',
                                           'infeasible',
                                           'infeasible',
                                           'infeasible',
                                           'infeasible'],
                          'sweep_params': {'fs.input[a]': {'lower bound': 0,
                                                           'units': 'non-dimensional',
                                                           'upper bound': 0,
                                                           'value': np.array([0.1, 0.1, 0.1, 0.5, 0.5, 0.5, 0.9, 0.9, 0.9])},
                                           'fs.input[b]': {'lower bound': 0,
                                                           'units': 'non-dimensional',
                                                           'upper bound': 0,
                                                           'value': np.array([0., 0.25, 0.5 , 0., 0.25, 0.5 , 0., 0.25, 0.5 ])}}}

            h5_fpath = os.path.join(tmp_path, 'output_dict.h5')
            read_dict = _read_output_h5(h5_fpath)
            _assert_dictionary_correctness(truth_dict, read_dict)

            # Check this new dictionary against the original logging system
            assert np.allclose(read_dict['outputs']['fs.input[a]']['value'], data[:,0], equal_nan=True)
            assert np.allclose(read_dict['sweep_params']['fs.input[a]']['value'], data[:,0], equal_nan=True)
            assert np.allclose(read_dict['outputs']['fs.input[b]']['value'], data[:,1], equal_nan=True)
            assert np.allclose(read_dict['sweep_params']['fs.input[b]']['value'], data[:,1], equal_nan=True)
            assert np.allclose(read_dict['outputs']['fs.output[c]']['value'], data[:,2], equal_nan=True)
            assert np.allclose(read_dict['outputs']['fs.output[d]']['value'], data[:,3], equal_nan=True)
            assert np.allclose(read_dict['outputs']['fs.performance']['value'], data[:,4], equal_nan=True)


    @pytest.mark.component
    def test_parameter_sweep_optimize(self, model, tmp_path):
        comm, rank, num_procs = _init_mpi()
        tmp_path = _get_rank0_path(comm, tmp_path)

        m = model
        m.fs.slack_penalty = 1000.
        m.fs.slack.setub(0)

        sweep_params = {'input_a' : (m.fs.input['a'], 0.1, 0.9, 3),
                        'input_b' : (m.fs.input['b'], 0.0, 0.5, 3)}
        outputs = {'output_c':m.fs.output['c'],
                   'output_d':m.fs.output['d'],
                   'performance':m.fs.performance,
                   'objective':m.objective}
        results_file = os.path.join(tmp_path, 'global_results_optimize.csv')
        h5_fname = "output_dict_optimize"

        # Call the parameter_sweep function
        parameter_sweep(m, sweep_params, outputs,
                csv_results_file = results_file,
                results_fname = h5_fname,
                optimize_function=_optimization,
                optimize_kwargs={'relax_feasibility':True},
                mpi_comm = comm)
        # parameter_sweep(m, sweep_params, outputs,
        #         results_file = results_file,
        #         optimize_function=_optimization,
        #         optimize_kwargs={'relax_feasibility':True},
        #         mpi_comm = comm)

        # NOTE: rank 0 "owns" tmp_path, so it needs to be
        #       responsible for doing any output file checking
        #       tmp_path can be deleted as soon as this method
        #       returns
        if rank == 0:
            # Check that the global results file is created
            assert os.path.isfile(results_file)

            # Attempt to read in the data
            data = np.genfromtxt(results_file, skip_header=1, delimiter=',')
            # Compare the last row of the imported data to truth
            truth_data = [ 0.9, 0.5, 1.0, 1.0, 2.0, 2.0 - 1000.*((2.*0.9 - 1.) + (3.*0.5 - 1.))]
            assert np.allclose(data[-1], truth_data, equal_nan=True)

        # Check the h5
        if rank == 0:

            truth_dict = {'outputs': {'fs.input[a]': {'lower bound': 0,
                                                      'units': 'non-dimensional',
                                                      'upper bound': 0,
                                                      'value': np.array([0.1, 0.1, 0.1, 0.5, 0.5, 0.5, 0.9, 0.9, 0.9])},
                                      'fs.input[b]': {'lower bound': 0,
                                                      'units': 'non-dimensional',
                                                      'upper bound': 0,
                                                      'value': np.array([0., 0.25, 0.5, 0., 0.25, 0.5 , 0., 0.25, 0.5 ])},
                                      'fs.output[c]': {'lower bound': 0,
                                                       'units': 'non-dimensional',
                                                       'upper bound': 0,
                                                       'value': np.array([0.20000001, 0.20000001, 0.20000001, 1., 1., 1., 1., 1., 1.])},
                                      'fs.output[d]': {'lower bound': 0,
                                                       'units': 'non-dimensional',
                                                       'upper bound': 0,
                                                       'value': np.array([9.98580690e-09, 7.50000010e-01, 1.00000000e+00, 9.99872731e-09, 7.50000010e-01, 1.00000000e+00, 9.99860382e-09, 7.50000010e-01, 1.])},
                                      'fs.performance': {'lower bound': 0,
                                                         'units': 'non-dimensional',
                                                         'upper bound': 0,
                                                         'value': np.array([0.20000002, 0.95000002, 1.20000001, 1.00000001, 1.75000001, 2., 1.00000001, 1.75000001, 2.])},
                                      'fs.slack[ab_slack]': {'lower bound': 0,
                                                             'units': 'non-dimensional',
                                                             'upper bound': 0,
                                                             'value': np.array([0., 0., 0., 0., 0., 0., 0.79999999, 0.79999999, 0.79999999])},
                                      'fs.slack[cd_slack]': {'lower bound': 0,
                                                             'units': 'non-dimensional',
                                                             'upper bound': 0,
                                                             'value': np.array([0., 0., 0.49999999, 0., 0., 0.49999999, 0., 0., 0.49999999])},
                                      'objective': {'lower bound': 0,
                                                    'units': 'non-dimensional',
                                                    'upper bound': 0,
                                                    'value': np.array([ 2.00000020e-01,  9.50000020e-01, -4.98799990e+02,  1.00000001e+00,  1.75000001e+00, -4.97999990e+02, -7.98999990e+02, -7.98249990e+02, 2.0 - 1000.*((2.*0.9 - 1.) + (3.*0.5 - 1.))])}},
                          'solve_status': ['optimal',
                                           'optimal',
                                           'optimal',
                                           'optimal',
                                           'optimal',
                                           'optimal',
                                           'optimal',
                                           'optimal',
                                           'optimal'],
                          'sweep_params': {'fs.input[a]': {'lower bound': 0,
                                                           'units': 'non-dimensional',
                                                           'upper bound': 0,
                                                           'value': np.array([0.1, 0.1, 0.1, 0.5, 0.5, 0.5, 0.9, 0.9, 0.9])},
                                           'fs.input[b]': {'lower bound': 0,
                                                           'units': 'non-dimensional',
                                                           'upper bound': 0,
                                                           'value': np.array([0., 0.25, 0.5 , 0., 0.25, 0.5, 0., 0.25, 0.5 ])}}}

            h5_fpath = os.path.join(tmp_path, '{0}.h5'.format(h5_fname))
            read_dict = _read_output_h5(h5_fpath)
            _assert_dictionary_correctness(truth_dict, read_dict)

            # Check this new dictionary against the original logging system
            assert np.allclose(read_dict['outputs']['fs.input[a]']['value'], data[:,0], equal_nan=True)
            assert np.allclose(read_dict['sweep_params']['fs.input[a]']['value'], data[:,0], equal_nan=True)
            assert np.allclose(read_dict['outputs']['fs.input[b]']['value'], data[:,1], equal_nan=True)
            assert np.allclose(read_dict['sweep_params']['fs.input[b]']['value'], data[:,1], equal_nan=True)
            assert np.allclose(read_dict['outputs']['fs.output[c]']['value'], data[:,2], equal_nan=True)
            assert np.allclose(read_dict['outputs']['fs.output[d]']['value'], data[:,3], equal_nan=True)
            assert np.allclose(read_dict['outputs']['fs.performance']['value'], data[:,4], equal_nan=True)
            assert np.allclose(read_dict['outputs']['objective']['value'], data[:,5], equal_nan=True)


    @pytest.mark.component
    def test_parameter_sweep_recover(self, model, tmp_path):
        comm, rank, num_procs = _init_mpi()
        tmp_path = _get_rank0_path(comm, tmp_path)

        m = model
        m.fs.slack_penalty = 1000.
        m.fs.slack.setub(0)

        sweep_params = {'input_a' : (m.fs.input['a'], 0.1, 0.9, 3),
                        'input_b' : (m.fs.input['b'], 0.0, 0.5, 3)}
        outputs = {'output_c':m.fs.output['c'],
                   'output_d':m.fs.output['d'],
                   'performance':m.fs.performance,
                   'objective':m.objective}
        results_file = os.path.join(tmp_path, 'global_results_recover.csv')
        h5_fname = "output_dict_recover"

        # Call the parameter_sweep function
        parameter_sweep(m, sweep_params, outputs,
                csv_results_file = results_file,
                results_fname = h5_fname,
                optimize_function=_optimization,
                reinitialize_function=_reinitialize,
                reinitialize_kwargs={'slack_penalty':10.},
                mpi_comm = comm)

        # parameter_sweep(m, sweep_params, outputs,
        #         results_file = results_file,
        #         optimize_function=_optimization,
        #         reinitialize_function=_reinitialize,
        #         reinitialize_kwargs={'slack_penalty':10.},
        #         mpi_comm = comm)

        # NOTE: rank 0 "owns" tmp_path, so it needs to be
        #       responsible for doing any output file checking
        #       tmp_path can be deleted as soon as this method
        #       returns
        if rank == 0:
            # Check that the global results file is created
            assert os.path.isfile(results_file)

            # Attempt to read in the data
            data = np.genfromtxt(results_file, skip_header=1, delimiter=',')

            # Compare the last row of the imported data to truth
            truth_data = [ 0.9, 0.5, 1.0, 1.0, 2.0, 2.0 - 10.*((2.*0.9 - 1.) + (3.*0.5 - 1.))]
            assert np.allclose(data[-1], truth_data, equal_nan=True)

        if rank == 0:

            truth_dict = {'outputs': {'fs.input[a]': {'lower bound': 0,
                                                      'units': 'non-dimensional',
                                                      'upper bound': 0,
                                                      'value': np.array([0.1, 0.1, 0.1, 0.5, 0.5, 0.5, 0.9, 0.9, 0.9])},
                                      'fs.input[b]': {'lower bound': 0,
                                                      'units': 'non-dimensional',
                                                      'upper bound': 0,
                                                      'value': np.array([0., 0.25, 0.5, 0., 0.25, 0.5, 0., 0.25, 0.5 ])},
                                      'fs.output[c]': {'lower bound': 0,
                                                       'units': 'non-dimensional',
                                                       'upper bound': 0,
                                                       'value': np.array([0.2, 0.2, 0.20000001, 1., 1., 1., 1., 1., 1.])},
                                      'fs.output[d]': {'lower bound': 0,
                                                       'units': 'non-dimensional',
                                                       'upper bound': 0,
                                                       'value': np.array([0.00000000e+00, 7.50000000e-01, 1.00000000e+00, 9.77756334e-09, 7.50000010e-01, 1.00000000e+00, 9.98605188e-09, 7.50000010e-01, 1.00000000e+00])},
                                      'fs.performance': {'lower bound': 0,
                                                         'units': 'non-dimensional',
                                                         'upper bound': 0,
                                                         'value': np.array([0.2, 0.95, 1.20000001, 1.00000001, 1.75000001, 2., 1.00000001, 1.75000001, 2.])},
                                      'fs.slack[ab_slack]': {'lower bound': 0,
                                                             'units': 'non-dimensional',
                                                             'upper bound': 0,
                                                             'value': np.array([0., 0., 0., 0., 0., 0., 0.79999999, 0.79999999, 0.79999999])},
                                      'fs.slack[cd_slack]': {'lower bound': 0,
                                                             'units': 'non-dimensional',
                                                             'upper bound': 0,
                                                             'value': np.array([0., 0., 0.49999999, 0., 0., 0.49999999, 0., 0., 0.49999999])},
                                      'objective': {'lower bound': 0,
                                                    'units': 'non-dimensional',
                                                    'upper bound': 0,
                                                    'value': np.array([0.2, 0.95,  -3.79999989,   1.00000001,   1.75000001,  -2.9999999 ,  -6.99999989,  -6.24999989, -10.9999998 ])}},
                          'solve_status': ['optimal',
                                           'optimal',
                                           'optimal',
                                           'optimal',
                                           'optimal',
                                           'optimal',
                                           'optimal',
                                           'optimal',
                                           'optimal'],
                          'sweep_params': {'fs.input[a]': {'lower bound': 0,
                                                           'units': 'non-dimensional',
                                                           'upper bound': 0,
                                                           'value': np.array([0.1, 0.1, 0.1, 0.5, 0.5, 0.5, 0.9, 0.9, 0.9])},
                                           'fs.input[b]': {'lower bound': 0,
                                                           'units': 'non-dimensional',
                                                           'upper bound': 0,
                                                           'value': np.array([0., 0.25, 0.5 , 0., 0.25, 0.5 , 0., 0.25, 0.5])}}}

            h5_fpath = os.path.join(tmp_path, '{0}.h5'.format(h5_fname))
            read_dict = _read_output_h5(h5_fpath)
            _assert_dictionary_correctness(truth_dict, read_dict)

            # Check this new dictionary against the original logging system
            assert np.allclose(read_dict['outputs']['fs.input[a]']['value'], data[:,0], equal_nan=True)
            assert np.allclose(read_dict['sweep_params']['fs.input[a]']['value'], data[:,0], equal_nan=True)
            assert np.allclose(read_dict['outputs']['fs.input[b]']['value'], data[:,1], equal_nan=True)
            assert np.allclose(read_dict['sweep_params']['fs.input[b]']['value'], data[:,1], equal_nan=True)
            assert np.allclose(read_dict['outputs']['fs.output[c]']['value'], data[:,2], equal_nan=True)
            assert np.allclose(read_dict['outputs']['fs.output[d]']['value'], data[:,3], equal_nan=True)
            assert np.allclose(read_dict['outputs']['fs.performance']['value'], data[:,4], equal_nan=True)
            assert np.allclose(read_dict['outputs']['objective']['value'], data[:,5], equal_nan=True)


    @pytest.mark.component
    def test_parameter_sweep_bad_recover(self, model, tmp_path):
        comm, rank, num_procs = _init_mpi()
        tmp_path = _get_rank0_path(comm, tmp_path)

        m = model
        m.fs.slack_penalty = 1000.
        m.fs.slack.setub(0)

        sweep_params = {'input_a' : (m.fs.input['a'], 0.1, 0.9, 3),
                        'input_b' : (m.fs.input['b'], 0.0, 0.5, 3)}
        outputs = {'output_c':m.fs.output['c'],
                   'output_d':m.fs.output['d'],
                   'performance':m.fs.performance,
                   'objective':m.objective}
        results_file = os.path.join(tmp_path, 'global_results_bad_recover.csv')
        h5_fname = "output_dict_bad_recover"

        # Call the parameter_sweep function
        parameter_sweep(m, sweep_params, outputs,
                csv_results_file = results_file,
                results_fname = h5_fname,
                optimize_function=_optimization,
                reinitialize_function=_bad_reinitialize,
                reinitialize_kwargs={'slack_penalty':10.},
                mpi_comm = comm)

        # parameter_sweep(m, sweep_params, outputs,
        #         results_file = results_file,
        #         optimize_function=_optimization,
        #         reinitialize_function=_bad_reinitialize,
        #         reinitialize_kwargs={'slack_penalty':10.},
        #         mpi_comm = comm)

        # NOTE: rank 0 "owns" tmp_path, so it needs to be
        #       responsible for doing any output file checking
        #       tmp_path can be deleted as soon as this method
        #       returns
        if rank == 0:
            # Check that the global results file is created
            assert os.path.isfile(results_file)

            # Attempt to read in the data
            data = np.genfromtxt(results_file, skip_header=1, delimiter=',')

            # Compare the last row of the imported data to truth
            truth_data = [ 0.9, 0.5, np.nan, np.nan, np.nan, np.nan]
            assert np.allclose(data[-1], truth_data, equal_nan=True)

        if rank == 0:
            truth_dict = {'outputs': {'fs.input[a]': {'lower bound': 0,
                                                      'units': 'non-dimensional',
                                                      'upper bound': 0,
                                                      'value': np.array([0.1, 0.1, 0.1, 0.5, 0.5, 0.5, 0.9, 0.9, 0.9])},
                                      'fs.input[b]': {'lower bound': 0,
                                                      'units': 'non-dimensional',
                                                      'upper bound': 0,
                                                      'value': np.array([0.  , 0.25, 0.5 , 0.  , 0.25, 0.5 , 0.  , 0.25, 0.5 ])},
                                      'fs.output[c]': {'lower bound': 0,
                                                       'units': 'non-dimensional',
                                                       'upper bound': 0,
                                                       'value': np.array([0.2, 0.2, np.nan, 1. , 1. , np.nan, np.nan, np.nan, np.nan])},
                                      'fs.output[d]': {'lower bound': 0,
                                                       'units': 'non-dimensional',
                                                       'upper bound': 0,
                                                       'value': np.array([0.  , 0.75, np.nan, 0., 0.75,  np.nan,  np.nan,  np.nan,  np.nan])},
                                      'fs.performance': {'lower bound': 0,
                                                         'units': 'non-dimensional',
                                                         'upper bound': 0,
                                                         'value': np.array([0.2 , 0.95, np.nan, 1., 1.75,  np.nan,  np.nan,  np.nan,  np.nan])},
                                      'fs.slack[ab_slack]': {'lower bound': 0,
                                                             'units': 'non-dimensional',
                                                             'upper bound': 0,
                                                             'value': np.array([ 0.,  0., np.nan, 0.,  0., np.nan, np.nan, np.nan, np.nan])},
                                      'fs.slack[cd_slack]': {'lower bound': 0,
                                                             'units': 'non-dimensional',
                                                             'upper bound': 0,
                                                             'value': np.array([ 0.,  0., np.nan,  0.,  0., np.nan, np.nan, np.nan, np.nan])},
                                      'objective': {'lower bound': 0,
                                                    'units': 'non-dimensional',
                                                    'upper bound': 0,
                                                    'value': np.array([0.2 , 0.95, np.nan, 1., 1.75,  np.nan,  np.nan,  np.nan,  np.nan])}},
                          'solve_status': ['optimal',
                                           'optimal',
                                           'infeasible',
                                           'optimal',
                                           'optimal',
                                           'infeasible',
                                           'infeasible',
                                           'infeasible',
                                           'infeasible'],
                          'sweep_params': {'fs.input[a]': {'lower bound': 0,
                                                           'units': 'non-dimensional',
                                                           'upper bound': 0,
                                                           'value': np.array([0.1, 0.1, 0.1, 0.5, 0.5, 0.5, 0.9, 0.9, 0.9])},
                                           'fs.input[b]': {'lower bound': 0,
                                                           'units': 'non-dimensional',
                                                           'upper bound': 0,
                                                           'value': np.array([0.  , 0.25, 0.5 , 0.  , 0.25, 0.5 , 0.  , 0.25, 0.5 ])}}}

            h5_fpath = os.path.join(tmp_path, '{0}.h5'.format(h5_fname))
            read_dict = _read_output_h5(h5_fpath)
            _assert_dictionary_correctness(truth_dict, read_dict)

            # Check this new dictionary against the original logging system
            assert np.allclose(read_dict['outputs']['fs.input[a]']['value'], data[:,0], equal_nan=True)
            assert np.allclose(read_dict['sweep_params']['fs.input[a]']['value'], data[:,0], equal_nan=True)
            assert np.allclose(read_dict['outputs']['fs.input[b]']['value'], data[:,1], equal_nan=True)
            assert np.allclose(read_dict['sweep_params']['fs.input[b]']['value'], data[:,1], equal_nan=True)
            assert np.allclose(read_dict['outputs']['fs.output[c]']['value'], data[:,2], equal_nan=True)
            assert np.allclose(read_dict['outputs']['fs.output[d]']['value'], data[:,3], equal_nan=True)
            assert np.allclose(read_dict['outputs']['fs.performance']['value'], data[:,4], equal_nan=True)
            assert np.allclose(read_dict['outputs']['objective']['value'], data[:,5], equal_nan=True)


def _optimization(m, relax_feasibility=False):
    if relax_feasibility:
        m.fs.slack.setub(None)

    solver = pyo.SolverFactory('ipopt')
    results = solver.solve(m)

    return results

def _reinitialize(m, slack_penalty=10.):
    m.fs.slack.setub(None)
    m.fs.slack_penalty.value = slack_penalty

def _bad_reinitialize(m, **kwargs):
    pass

def _get_rank0_path(comm, tmp_path):
    if comm is None:
        return tmp_path
    return comm.bcast(tmp_path, root=0)

def _assert_dictionary_correctness(truth_dict, test_dict):

    for key, item in truth_dict.items():
        if key != 'solve_status':
            for subkey, subitem in item.items():
                assert subitem['lower bound'] == test_dict[key][subkey]['lower bound']
                assert subitem['upper bound'] == test_dict[key][subkey]['upper bound']
                assert subitem['units'] == test_dict[key][subkey]['units']
                assert np.allclose(test_dict[key][subkey]['value'], subitem['value'], equal_nan=True)
        elif key == "solve_status":
            assert item == test_dict[key]
