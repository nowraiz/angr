
import binascii
import os

from ..engines.soot.values import SimSootValue_Local, SimSootValue_ArrayRef, SimSootValue_ParamRef, \
                                  SimSootValue_StaticFieldRef, SimSootValue_InstanceFieldRef

from ..storage.memory import SimMemory
from .keyvalue_memory import SimKeyValueMemory
from .plugin import SimStatePlugin
from ..errors import SimUnsatError, SimMemoryAddressError
from .. import concretization_strategies
from .. import sim_options as options

import logging
l = logging.getLogger("angr.state_plugins.javavm_memory")

MAX_ARRAY_SIZE = 1000 # FIXME arbitrarily chosen limit

class SimJavaVmMemory(SimMemory):
    def __init__(self, memory_id="mem", stack=None, heap=None, vm_static_table=None,
                 load_strategies=[], store_strategies=[]):
        super(SimJavaVmMemory, self).__init__()

        self.id = memory_id

        self._stack = [ ] if stack is None else stack
        self.heap = SimKeyValueMemory("mem") if heap is None else heap
        self.vm_static_table = SimKeyValueMemory("mem") if vm_static_table is None else vm_static_table

        # Heap helper
        # TODO: ask someone how we want to manage this
        # TODO: Manage out of memory allocation
        # self._heap_allocation_id = 0
        self.max_array_size = MAX_ARRAY_SIZE

        # concretizing strategies
        self.load_strategies = load_strategies
        self.store_strategies = store_strategies

    def get_new_uuid(self):
        # self._heap_allocation_id += 1
        # return str(self._heap_allocation_id)
        return binascii.hexlify(os.urandom(4))

    def store(self, addr, data, size=None, condition=None, add_constraints=None, endness=None, action=None,
              inspect=True, priv=None, disable_actions=False, frame=0):

        if type(addr) is SimSootValue_Local:
            cstack = self._stack[-1+(-1*frame)]
            # A local variable
            # TODO: Implement the stacked stack frames model
            cstack.store(addr.id, data, type_=addr.type)

        elif type(addr) is SimSootValue_ArrayRef:
            self.store_array_elements(array=addr, start_idx=addr.index, data=data)

        elif type(addr) is SimSootValue_StaticFieldRef:
            self.vm_static_table.store(addr.id, data, type_=addr.type)
        elif type(addr) is SimSootValue_InstanceFieldRef:
            self.heap.store(addr.id, data, type_=addr.type)
        else:
            l.error("Unknown addr type %s" % addr)

    def load(self, addr, size=None, condition=None, fallback=None, add_constraints=None, action=None, endness=None,
             inspect=True, disable_actions=False, ret_on_segv=False, none_if_missing=False, frame=0):

        if type(addr) is SimSootValue_Local:
            cstack = self._stack[-1+(-1*frame)]
            # Load a local variable
            # TODO: Implement the stacked stack frames model
            return cstack.load(addr.id, none_if_missing=True)

        elif type(addr) is SimSootValue_ArrayRef:
            return self.load_array_elements(array=addr, start_idx=addr.index, no_of_elements=1)[0]

        elif type(addr) is SimSootValue_ParamRef:
            cstack = self._stack[-1+(-1*frame)]
            # Load a local variable
            # TODO: Implement the stacked stack frames model
            return cstack.load(addr.id, none_if_missing=True)

        elif type(addr) is SimSootValue_StaticFieldRef:
            return self.vm_static_table.load(addr.id, none_if_missing=True)

        elif type(addr) is SimSootValue_InstanceFieldRef:
            return self.heap.load(addr.id, none_if_missing=True)

        else:
            l.error("Unknown addr type %s" % addr)
            return None

    def push_stack_frame(self):
        self._stack.append(SimKeyValueMemory("mem"))

    def pop_stack_frame(self):
        self._stack = self._stack[:-1]

    @property
    def stack(self):
        return self._stack[-1]


    #
    # Array // Store
    #

    def store_array_elements(self, array, start_idx, data):

        """
        Stores either a single element or a range of elements in the array.

        :param array:     Reference to the array (SimSootValue_ArrayRef).
        :param start_idx: Starting index for the store.
        :param data:      Either a single value or a list of values.
        """
        
        # we process data as a list of elements
        # => if there is only a single element, wrap it in a list
        data = data if isinstance(data, list) else [data]

        # concretize start index
        concrete_start_idxes = self.concretize_store_idx(start_idx)

        if len(concrete_start_idxes) == 1:
            # only one start index
            # => concrete store
            concrete_start_idx = concrete_start_idxes[0]
            for i, value in enumerate(data):
                self._store_array_element_on_heap(array=array, 
                                          idx=concrete_start_idx+i,
                                          value=value,
                                          value_type=array.type)
            # constraint the start idx to the concrete one, in case
            # the index was symbolic prior to the concretization
            self.state.solver.add(concrete_start_idx == start_idx)

        else:
            # multiple start indexes
            # => symbolic store
            start_idx_options = []
            for concrete_start_idx in concrete_start_idxes:
                start_idx_options.append(concrete_start_idx == start_idx)
                # we store elements condtioned with the start index:
                # => if concrete_start_idx == start_idx
                #    then store the value
                #    else keep the current value
                for i, value in enumerate(data):
                    self._store_array_element_on_heap(array=array, 
                                                      idx=concrete_start_idx+i,
                                                      value=value,
                                                      value_type=array.type,
                                                      store_condition=start_idx_options[-1])

            # constraint start_idx, s.t. it evals to one of the concretized indexes
            constraint_on_start_idx = self.state.solver.Or(*start_idx_options)
            self.state.add_constraints(constraint_on_start_idx)

    def _store_array_element_on_heap(self, array, idx, value, value_type, store_condition=None):
        heap_elem_id = '%s[%d]' % (array.heap_alloc_id, idx)
        l.debug("Set {heap_elem_id} to {value} with condition {store_condition}".format(
                 heap_elem_id=heap_elem_id, value=value, store_condition=store_condition))
        if store_condition is not None:
            current_value = self._load_array_element_from_heap(array, idx)
            new_value = value
            value = self.state.solver.If(store_condition, new_value, current_value)
        self.heap.store(heap_elem_id, value, value_type)

    #
    # Array // Load
    #

    def load_array_elements(self, array, start_idx, no_of_elements):

        """
        Loads either a single element or a range of elements from the array.

        :param array:           Reference to the array (SimSootValue_ArrayRef).
        :param start_idx:       Starting index for the load.
        :param no_of_elements:  Number of elements to load.

        """

        # concretize start index
        concrete_start_idxes = self.concretize_load_idx(start_idx)

        if len(concrete_start_idxes) == 1:
            # only one start index
            # => concrete load
            concrete_start_idx = concrete_start_idxes[0]
            load_values = [self._load_array_element_from_heap(array, idx) 
                           for idx in range(concrete_start_idx, concrete_start_idx+no_of_elements)]
            # constraint the start idx to the concrete one, in case
            # the index was symbolic prior to the concretization
            self.state.solver.add(start_idx == concrete_start_idx)
        
        else:
            # multiple start indexes
            # => symbolic load

            # start with load values for the first concrete index
            concrete_start_idx = concrete_start_idxes[0]
            load_values = [self._load_array_element_from_heap(array, idx) 
                           for idx in range(concrete_start_idx, concrete_start_idx+no_of_elements)]
            start_idx_options = [concrete_start_idx == start_idx]

            # update load values with all remaining start indexes
            for concrete_start_idx in concrete_start_idxes[1:]:
                # load values for this start index
                values = [self._load_array_element_from_heap(array, idx) 
                        for idx in range(concrete_start_idx, concrete_start_idx+no_of_elements)]
                # update load values with the new ones
                for i, value in enumerate(values):
                    # condition every value with the start idx
                    # => if concrete_start_idx == start_idx
                    #    then use new value
                    #    else use the current value
                    load_values[i] = self.state.solver.If(
                        concrete_start_idx == start_idx,
                        value,
                        load_values[i]
                    )
                start_idx_options.append(start_idx == concrete_start_idx)

            # constraint start_idx, s.t. it evals to one of the concretized indexes
            constraint_on_start_idx = self.state.solver.Or(*start_idx_options)
            self.state.add_constraints(constraint_on_start_idx)

        return load_values

    def _load_array_element_from_heap(self, array, idx):
        # try to load the element
        heap_elem_id = '%s[%d]' % (array.heap_alloc_id, idx)
        value = self.heap.load(heap_elem_id, none_if_missing=True)
        # if it's not available, initialize it
        if value is None:
            value = self.state.project.simos.get_default_value_by_type(array.type)
            l.debug("Init {heap_elem_id} with {value}".format(
                     heap_elem_id=heap_elem_id, value=value))
            self.heap.store(heap_elem_id, value)
        else:
            l.debug("Load {value} from {heap_elem_id}".format(
                     heap_elem_id=heap_elem_id, value=value))
        return value

    #
    # Concretization strategies
    #

    def _apply_concretization_strategies(self, idx, strategies, action):
        """
        Applies concretization strategies on the index until one of them succeeds.
        """

        for s in strategies:
            try:
                idxes = s.concretize(self, idx)
            except SimUnsatError:
                idxes = None

            if idxes:
                return idxes
        else:
            raise SimMemoryAddressError("Unable to concretize index %s" % str(idx))
        
    def concretize_store_idx(self, idx, strategies=None):
        """
        Concretizes a store index.

        :param idx:             An expression for the index.
        :param strategies:      A list of concretization strategies (to override the default).
        :param min_idx:         Minimum value for a concretized index (inclusive).
        :param max_idx:         Maximum value for a concretized index (exclusive).
        :returns:               A list of concrete indexes.
        """
        if isinstance(idx, int):
            return [ idx ]
        elif not self.state.solver.symbolic(idx):
            return [ self.state.solver.eval(idx) ]

        strategies = self.store_strategies if strategies is None else strategies
        return self._apply_concretization_strategies(idx, strategies, 'store')

    def concretize_load_idx(self, idx, strategies=None):
        """
        Concretizes a load index.

            :param idx:             An expression for the index.
            :param strategies:      A list of concretization strategies (to override the default).
            :param min_idx:         Minimum value for a concretized index (inclusive).
            :param max_idx:         Maximum value for a concretized index (exclusive).
            :returns:               A list of concrete indexes.
        """

        if isinstance(idx, int):
            return [ idx ]
        elif not self.state.solver.symbolic(idx):
            return [ self.state.se.eval(idx) ]

        strategies = self.load_strategies if strategies is None else strategies
        return self._apply_concretization_strategies(idx, strategies, 'load')

    def _create_default_load_strategies(self):
        # reset dict
        self.load_strategies = []

        # symbolically read up to 1024 elements
        s = concretization_strategies.SimConcretizationStrategyRange(1024)
        self.load_strategies.append(s)

        # if range is too big, fallback to load only one arbitrary element
        s = concretization_strategies.SimConcretizationStrategyAny()
        self.load_strategies.append(s)

    def _create_default_store_strategies(self):
        # reset dict
        self.store_strategies = []

        # symbolically write up to 256 elements
        s = concretization_strategies.SimConcretizationStrategyRange(256)
        self.store_strategies.append(s)

        # if range is too big, fallback to store only the last element
        s = concretization_strategies.SimConcretizationStrategyMax()
        self.store_strategies.append(s)

    #
    # MISC
    #

    def set_state(self, state):
        super(SimJavaVmMemory, self).set_state(state)
        if not self.load_strategies:
            self._create_default_load_strategies()
        if not self.store_strategies:
            self._create_default_store_strategies()

    @SimStatePlugin.memo
    def copy(self, _):
        return SimJavaVmMemory(
            memory_id=self.id,
            stack=[stack_frame.copy() for stack_frame in self._stack],
            heap=self.heap.copy(),
            vm_static_table=self.vm_static_table.copy(),
            load_strategies=[s.copy() for s in self.load_strategies],
            store_strategies=[s.copy() for s in self.store_strategies]
        )


from angr.sim_state import SimState
SimState.register_default('javavm_memory', SimJavaVmMemory)
