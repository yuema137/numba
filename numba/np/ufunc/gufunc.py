from numba import typeof
from numba.core import types
from numba.np.ufunc.ufuncbuilder import GUFuncBuilder
from numba.np.ufunc.sigparse import parse_signature
from numba.np.ufunc.ufunc_base import UfuncBase, UfuncLowererBase
from numba.np.numpy_support import ufunc_find_matching_loop
from numba.core import serialize, errors
from numba.core.typing import npydecl
from numba.core.typing.templates import signature
import functools


def make_gufunc_kernel(_dufunc):
    from numba.np import npyimpl

    class GUFuncKernel(npyimpl._Kernel):
        """
        npyimpl._Kernel subclass responsible for lowering a gufunc kernel
        (element-wise function) inside a broadcast loop (which is
        generated by npyimpl.numpy_gufunc_kernel()).
        """
        dufunc = _dufunc

        def __init__(self, context, builder, outer_sig):
            super().__init__(context, builder, outer_sig)
            ewise_types = self.dufunc._get_ewise_dtypes(outer_sig.args)
            self.inner_sig, self.cres = self.dufunc.find_ewise_function(
                ewise_types)

        def cast(self, val, fromty, toty):
            # Handle the case where "fromty" is an array and "toty" a scalar
            if isinstance(fromty, types.Array) and not \
                    isinstance(toty, types.Array):
                return super().cast(val, fromty.dtype, toty)
            return super().cast(val, fromty, toty)

        def generate(self, *args):
            if self.cres.objectmode:
                msg = ('Calling a guvectorize function in object mode is not '
                       'supported yet.')
                raise errors.NumbaRuntimeError(msg)
            self.context.add_linking_libs((self.cres.library,))
            return super().generate(*args)

    GUFuncKernel.__name__ += _dufunc.__name__
    return GUFuncKernel


class GUFuncLowerer(UfuncLowererBase):
    '''Callable class responsible for lowering calls to a specific gufunc.
    '''
    def __init__(self, gufunc):
        from numba.np import npyimpl
        super().__init__(gufunc,
                         make_gufunc_kernel,
                         npyimpl.numpy_gufunc_kernel)


class GUFunc(serialize.ReduceMixin, UfuncBase):
    """
    Dynamic generalized universal function (GUFunc)
    intended to act like a normal Numpy gufunc, but capable
    of call-time (just-in-time) compilation of fast loops
    specialized to inputs.
    """

    def __init__(self, py_func, signature, identity=None, cache=None,
                 is_dynamic=False, targetoptions={}, writable_args=()):
        self.ufunc = None
        self._frozen = False
        self._is_dynamic = is_dynamic
        self._identity = identity

        # GUFunc cannot inherit from GUFuncBuilder because "identity"
        # is a property of GUFunc. Thus, we hold a reference to a GUFuncBuilder
        # object here
        self.gufunc_builder = GUFuncBuilder(
            py_func, signature, identity, cache, targetoptions, writable_args)

        self.__name__ = self.gufunc_builder.py_func.__name__
        self.__doc__ = self.gufunc_builder.py_func.__doc__
        self._dispatcher = self.gufunc_builder.nb_func
        self._initialize(self._dispatcher)
        functools.update_wrapper(self, py_func)

    def _initialize(self, dispatcher):
        self.build_ufunc()
        self._install_type()
        self._lower_me = GUFuncLowerer(self)
        self._install_cg()

    def _reduce_states(self):
        gb = self.gufunc_builder
        dct = dict(
            py_func=gb.py_func,
            signature=gb.signature,
            identity=self._identity,
            cache=gb.cache,
            is_dynamic=self._is_dynamic,
            targetoptions=gb.targetoptions,
            writable_args=gb.writable_args,
            typesigs=gb._sigs,
            frozen=self._frozen,
        )
        return dct

    @classmethod
    def _rebuild(cls, py_func, signature, identity, cache, is_dynamic,
                 targetoptions, writable_args, typesigs, frozen):
        self = cls(py_func=py_func, signature=signature, identity=identity,
                   cache=cache, is_dynamic=is_dynamic,
                   targetoptions=targetoptions, writable_args=writable_args)
        for sig in typesigs:
            self.add(sig)
        self.build_ufunc()
        self._frozen = frozen
        return self

    def __repr__(self):
        return f"<numba._GUFunc '{self.__name__}'>"

    def add(self, fty):
        self.gufunc_builder.add(fty)

    def build_ufunc(self):
        self.ufunc = self.gufunc_builder.build_ufunc()
        return self

    def expected_ndims(self):
        parsed_sig = parse_signature(self.gufunc_builder.signature)
        return tuple(map(len, parsed_sig[0])) + tuple(map(len, parsed_sig[1]))

    def _type_me(self, argtys, kws):
        """
        Implement AbstractTemplate.generic() for the typing class
        built by gufunc._install_type().

        Return the call-site signature after either validating the
        element-wise signature or compiling for it.
        """
        assert not kws
        ufunc = self.ufunc
        _handle_inputs_result = npydecl.Numpy_rules_ufunc._handle_inputs(
            ufunc, argtys, kws)
        ewise_types, _, _, _ = _handle_inputs_result
        sig, _ = self.find_ewise_function(ewise_types)

        if sig is None:
            # Matching element-wise signature was not found; must
            # compile.
            if self._frozen:
                raise TypeError("cannot call %s with types %s"
                                % (self, argtys))
            self._compile_for_argtys(ewise_types)
            # double check to ensure there is a match
            sig, _ = self.find_ewise_function(ewise_types)
            assert sig is not None

        return signature(types.none, *argtys)

    def _compile_for_argtys(self, argtys, return_type=None):
        # Compile a new guvectorize function! Use the gufunc signature
        # i.e. (n,m),(m)->(n)
        # plus ewise_types to build a numba function type
        fnty = self._get_signature(*argtys)
        self.gufunc_builder.add(fnty)

    def match_signature(self, ewise_types, sig):
        dtypes = self._get_ewise_dtypes(sig.args)
        return tuple(dtypes) == tuple(ewise_types)

    @property
    def is_dynamic(self):
        return self._is_dynamic

    def _get_ewise_dtypes(self, args):
        argtys = map(lambda arg: arg if isinstance(arg, types.Type) else
                     typeof(arg), args)
        tys = []
        for argty in argtys:
            if isinstance(argty, types.Array):
                tys.append(argty.dtype)
            else:
                tys.append(argty)
        return tys

    def _num_args_match(self, *args):
        parsed_sig = parse_signature(self.gufunc_builder.signature)
        # parsed_sig[1] has always length 1
        return len(args) == len(parsed_sig[0]) + 1

    def _get_signature(self, *args):
        parsed_sig = parse_signature(self.gufunc_builder.signature)
        # ewise_types is a list of [int32, int32, int32, ...]
        ewise_types = self._get_ewise_dtypes(args)

        # first time calling the gufunc
        # generate a signature based on input arguments
        l = []
        for idx, sig_dim in enumerate(parsed_sig[0]):
            ndim = len(sig_dim)
            if ndim == 0:  # append scalar
                l.append(ewise_types[idx])
            else:
                l.append(types.Array(ewise_types[idx], ndim, 'A'))

        # add return type to signature
        retty = ewise_types[-1]
        ret_ndim = len(parsed_sig[-1][0]) or 1  # small hack to return scalar
        l.append(types.Array(retty, ret_ndim, 'A'))

        return types.none(*l)

    def __call__(self, *args, **kwargs):
        # If compilation is disabled OR it is NOT a dynamic gufunc
        # call the underlying gufunc
        if self._frozen or not self.is_dynamic:
            return self.ufunc(*args, **kwargs)
        elif "out" in kwargs:
            # If "out" argument is supplied
            args += (kwargs.pop("out"),)

        if self._num_args_match(*args) is False:
            # It is not allowed to call a dynamic gufunc without
            # providing all the arguments
            # see: https://github.com/numba/numba/pull/5938#discussion_r506429392  # noqa: E501
            msg = (
                f"Too few arguments for function '{self.__name__}'. "
                "Note that the pattern `out = gufunc(Arg1, Arg2, ..., ArgN)` "
                "is not allowed. Use `gufunc(Arg1, Arg2, ..., ArgN, out) "
                "instead.")
            raise TypeError(msg)

        # at this point we know the gufunc is a dynamic one
        ewise = self._get_ewise_dtypes(args)
        if not (self.ufunc and ufunc_find_matching_loop(self.ufunc, ewise)):
            sig = self._get_signature(*args)
            self.add(sig)
            self.build_ufunc()
        return self.ufunc(*args, **kwargs)
