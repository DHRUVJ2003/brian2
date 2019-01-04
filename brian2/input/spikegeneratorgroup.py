'''
Module defining `SpikeGeneratorGroup`.
'''
import numpy as np

from brian2.core.functions import timestep
from brian2.utils.logger import get_logger
from brian2.core.spikesource import SpikeSource
from brian2.units.fundamentalunits import check_units, Unit, Quantity
from brian2.units.allunits import second
from brian2.core.variables import Variables
from brian2.groups.group import CodeRunner, Group

__all__ = ['SpikeGeneratorGroup']


logger = get_logger(__name__)


class SpikeGeneratorGroup(Group, CodeRunner, SpikeSource):
    '''
    SpikeGeneratorGroup(N, indices, times, dt=None, clock=None,
                        period=1e100*second, when='thresholds', order=0,
                        sorted=False, name='spikegeneratorgroup*',
                        codeobj_class=None)

    A group emitting spikes at given times.

    Parameters
    ----------
    N : int
        The number of "neurons" in this group
    indices : array of integers
        The indices of the spiking cells
    times : `Quantity`
        The spike times for the cells given in ``indices``. Has to have the
        same length as ``indices``.
    period : `Quantity`, optional
        If this is specified, it will repeat spikes with this period.
    dt : `Quantity`, optional
        The time step to be used for the simulation. Cannot be combined with
        the `clock` argument.
    clock : `Clock`, optional
        The update clock to be used. If neither a clock, nor the `dt` argument
        is specified, the `defaultclock` will be used.
    when : str, optional
        When to run within a time step, defaults to the ``'thresholds'`` slot.
    order : int, optional
        The priority of of this group for operations occurring at the same time
        step and in the same scheduling slot. Defaults to 0.
    sorted : bool, optional
        Whether the given indices and times are already sorted. Set to ``True``
        if your events are already sorted (first by spike time, then by index),
        this can save significant time at construction if your arrays contain
        large numbers of spikes. Defaults to ``False``.

    Notes
    -----
    * In a time step, `SpikeGeneratorGroup` emits all spikes that happened
      at :math:`t-dt < t_{spike} \leq t`. This might lead to unexpected
      or missing spikes if you change the time step dt between runs.
    * `SpikeGeneratorGroup` does not currently raise any warning if a neuron
      spikes more that once during a time step, but other code (e.g. for
      synaptic propagation) might assume that neurons only spike once per
      time step and will therefore not work properly.
    * If `sorted` is set to ``True``, the given arrays will not be copied
      (only affects runtime mode)..
    '''

    @check_units(N=1, indices=1, times=second, period=second)
    def __init__(self, N, indices, times, dt=None, clock=None,
                 period=1e100*second, when='thresholds', order=0, sorted=False,
                 name='spikegeneratorgroup*', codeobj_class=None):

        Group.__init__(self, dt=dt, clock=clock, when=when, order=order, name=name)

        # We store the indices and times also directly in the Python object,
        # this way we can use them for checks in `before_run` even in standalone
        # TODO: Remove this when the checks in `before_run` have been moved to the template
        #: Array of spiking neuron indices.
        self._neuron_index = None
        #: Array of spiking neuron times.
        self._spike_time = None
        #: "Dirty flag" that will be set when spikes are changed after the
        #: `before_run` check
        self._spikes_changed = True

        # Let other objects know that we emit spikes events
        self.events = {'spike': None}

        self.codeobj_class = codeobj_class

        if N < 1 or int(N) != N:
            raise TypeError('N has to be an integer >=1.')
        N = int(N)  # Make sure that it is an integer, values such as 10.0 would
                    # otherwise make weave compilation fail
        self.start = 0
        self.stop = N

        self.variables = Variables(self)
        self.variables.create_clock_variables(self._clock)

        indices, times = self._check_args(indices, times, period, N, sorted)

        self.variables.add_constant('N', value=N)
        self.variables.add_array('period', dimensions=second.dim, size=1,
                                 constant=True, read_only=True, scalar=True,
                                 dtype=self._clock.variables['t'].dtype)
        self.variables.add_arange('i', N)
        self.variables.add_dynamic_array('spike_number',
                                         values=np.arange(len(indices)),
                                         size=len(indices),
                                         dtype=np.int32, read_only=True,
                                         constant=True, index='spike_number',
                                         unique=True)
        self.variables.add_dynamic_array('neuron_index', values=indices,
                                         size=len(indices),
                                         dtype=np.int32, index='spike_number',
                                         read_only=True, constant=True)
        self.variables.add_dynamic_array('spike_time', values=times, size=len(times),
                                         dimensions=second.dim, index='spike_number',
                                         read_only=True, constant=True,
                                         dtype=self._clock.variables['t'].dtype)
        self.variables.add_array('_spikespace', size=N+1, dtype=np.int32)
        self.variables.add_array('_lastindex', size=1, values=0, dtype=np.int32,
                                 read_only=True, scalar=True)

        #: Remember the dt we used the last time when we checked the spike bins
        #: to not repeat the work for multiple runs with the same dt
        self._previous_dt = None

        CodeRunner.__init__(self, self,
                            code='',
                            template='spikegenerator',
                            clock=self._clock,
                            when=when,
                            order=order,
                            name=None)

        # Activate name attribute access
        self._enable_group_attributes()

        self.variables['period'].set_value(period)

    def before_run(self, run_namespace):
        # Do some checks on the period vs. dt
        dt = self.dt_[:]  # make a copy
        period = self.period_
        if period < np.inf:
            if period < dt:
                raise ValueError('The period of %s is %s, which is smaller '
                                 'than its dt of %s.' % (self.name,
                                                         self.period[:],
                                                         dt*second))
            # We check that the period is a multiple of dt. Because we cannot do
            # any exact test, we use the following heuristic: we calculate all
            # the multiples of the period that fit into 1000s and then check
            # whether all these times correspond to the same time step as we
            # obtain by first converting the period to the equivalent number of
            # timesteps
            if period < 1000:
                n_periods = int(1000. / period)
                periods = np.arange(n_periods) * period
                if not np.all(timestep(periods*second, dt*second) % timestep(period*second, dt*second) == 0):
                    raise NotImplementedError('The period of %s is %s, which is '
                                              'not an integer multiple of its dt '
                                              'of %s.' % (self.name,
                                                          self.period[:],
                                                          dt*second))

        if self._spikes_changed:
            current_t = self.variables['t'].get_value().item()
            timesteps = timestep(self._spike_time, dt)
            current_step = timestep(current_t, dt)
            in_the_past = np.nonzero(timesteps < current_step)[0]
            if len(in_the_past):
                logger.warn('The SpikeGeneratorGroup contains spike times '
                            'earlier than the start time of the current run '
                            '(t = {}), these spikes will be '
                            'ignored.'.format(str(current_t*second)),
                            name_suffix='ignored_spikes')
                self.variables['_lastindex'].set_value(in_the_past[-1] + 1)
            else:
                self.variables['_lastindex'].set_value(0)

        # Check that we don't have more than one spike per neuron in a time bin
        if self._previous_dt is None or dt != self._previous_dt or self._spikes_changed:
            # We shift all the spikes by a tiny amount to make sure that spikes
            # at exact multiples of dt do not end up in the previous time bin
            # This shift has to be quite significant relative to machine
            # epsilon, we use 1e-3 of the dt here
            shift = 1e-3*dt
            timebins = np.asarray(np.asarray(self._spike_time + shift)/dt,
                                  dtype=np.int32)
            # time is already in sorted order, so it's enough to check if the condition
            # that timebins[i]==timebins[i+1] and self._neuron_index[i]==self._neuron_index[i+1]
            # is ever both true
            if (np.logical_and(np.diff(timebins)==0, np.diff(self._neuron_index)==0)).any():
                raise ValueError('Using a dt of %s, some neurons of '
                                 'SpikeGeneratorGroup "%s" spike more than '
                                 'once during a time step.' % (str(self.dt),
                                                               self.name))
            self._previous_dt = dt
            self._spikes_changed = False

        super(SpikeGeneratorGroup, self).before_run(run_namespace=run_namespace)

    @check_units(indices=1, times=second, period=second)
    def set_spikes(self, indices, times, period=1e100*second, sorted=False):
        '''
        set_spikes(indices, times, period=1e100*second, sorted=False)

        Change the spikes that this group will generate.

        This can be used to set the input for a second run of a model based on
        the output of a first run (if the input for the second run is already
        known before the first run, then all the information should simply be
        included in the initial `SpikeGeneratorGroup` initializer call,
        instead).

        Parameters
        ----------
        indices : array of integers
            The indices of the spiking cells
        times : `Quantity`
            The spike times for the cells given in ``indices``. Has to have the
            same length as ``indices``.
        period : `Quantity`, optional
            If this is specified, it will repeat spikes with this period.
        sorted : bool, optional
            Whether the given indices and times are already sorted. Set to
            ``True`` if your events are already sorted (first by spike time,
            then by index), this can save significant time at construction if
            your arrays contain large numbers of spikes. Defaults to ``False``.
        '''

        indices, times = self._check_args(indices, times, period, self.N, sorted)

        self.variables['period'].set_value(period)
        self.variables['neuron_index'].resize(len(indices))
        self.variables['spike_time'].resize(len(indices))
        self.variables['spike_number'].resize(len(indices))
        self.variables['spike_number'].set_value(np.arange(len(indices)))
        self.variables['neuron_index'].set_value(indices)
        self.variables['spike_time'].set_value(times)
        # _lastindex will be set as part of before_run

    def _check_args(self, indices, times, period, N, sorted):
        times = Quantity(times)
        if len(indices) != len(times):
            raise ValueError(('Length of the indices and times array must '
                              'match, but %d != %d') % (len(indices),
                                                        len(times)))
        if period < 0*second:
            raise ValueError('The period cannot be negative.')
        elif len(times) and period <= np.max(times):
            raise ValueError('The period has to be greater than the maximum of '
                             'the spike times')
        if len(times) and np.min(times) < 0*second:
            raise ValueError('Spike times cannot be negative')
        if len(indices) and (np.min(indices) < 0 or np.max(indices) >= N):
            raise ValueError('Indices have to lie in the interval [0, %d[' % N)

        times = np.asarray(times)
        indices = np.asarray(indices)
        if not sorted:
            # sort times and indices first by time, then by indices
            I = np.lexsort((indices, times))
            indices = indices[I]
            times = times[I]

        # We store the indices and times also directly in the Python object,
        # this way we can use them for checks in `before_run` even in standalone
        # TODO: Remove this when the checks in `before_run` have been moved to the template
        self._neuron_index = indices
        self._spike_time = times
        #: "Dirty flag" that will be set when spikes are changed after the
        #: `before_run` check
        self._spikes_changed = True

        return indices, times

    @property
    def spikes(self):
        '''
        The spikes returned by the most recent thresholding operation.
        '''
        # Note that we have to directly access the ArrayVariable object here
        # instead of using the Group mechanism by accessing self._spikespace
        # Using the latter would cut _spikespace to the length of the group
        spikespace = self.variables['_spikespace'].get_value()
        return spikespace[:spikespace[-1]]

    def __repr__(self):
        return ('{cls}({N}, indices=<length {l} array>, '
                'times=<length {l} array>').format(cls=self.__class__.__name__,
                                                   N=self.N,
                                                   l=self.variables['neuron_index'].size)
