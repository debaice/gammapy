# Licensed under a 3-clause BSD style license - see LICENSE.rst
import numpy as np
from scipy.optimize import minimize, brentq
from .likelihood import Likelihood


__all__ = ["optimize_scipy", "covariance_scipy", "confidence_scipy"]


def optimize_scipy(parameters, function, **kwargs):
    method = kwargs.pop("method", "Nelder-Mead")
    pars = [par.factor for par in parameters.free_parameters]

    bounds = []
    for par in parameters.free_parameters:
        parmin = par.factor_min if not np.isnan(par.factor_min) else None
        parmax = par.factor_max if not np.isnan(par.factor_max) else None
        bounds.append((parmin, parmax))

    likelihood = Likelihood(function, parameters)
    result = minimize(likelihood.fcn, pars, bounds=bounds, method=method, **kwargs)

    factors = result.x
    info = {"success": result.success, "message": result.message, "nfev": result.nfev}
    optimizer = None

    return factors, info, optimizer


class TSDifference:
    """Likelihood wrapper to compute TS differences"""
    def __init__(self, function, parameters, parameter, ts_diff):
        self.loglike_ref = function(parameters)
        self.parameters = parameters
        self.function = function
        self.parameter = parameter
        self.parameter.frozen = True
        self.ts_diff = ts_diff

    def fcn(self, factor):
        self.parameter.factor = factor
        optimize_scipy(self.parameters, self.function)
        value = self.function(self.parameters) - self.loglike_ref - self.ts_diff
        return value


def _confidence_scipy_brentq(parameters, parameter, function, sigma, upper=True, **kwargs):
    ts_diff = TSDifference(function, parameters, parameter, ts_diff=sigma ** 2)

    kwargs.setdefault("a", parameter.factor)

    bound = parameter.factor_max if upper else parameter.factor_min

    if np.isnan(bound):
        bound = parameter.factor
        if upper:
            bound += 1e2 * parameters.error(parameter) / parameter.scale
        else:
            bound -= 1e2 * parameters.error(parameter) / parameter.scale

    kwargs.setdefault("b", bound)

    message, success = "Confidence terminated successfully.", True

    try:
        result = brentq(ts_diff.fcn, full_output=True, **kwargs)
    except ValueError:
        message = ("Confidence estimation failed, because bracketing interval"
                   " does not contain a unique solution. Try setting the interval by hand.")
        success = False

    suffix = "errp" if upper else "errn"

    return {
        "nfev_" + suffix: result[1].iterations,
        suffix : np.abs(result[0] - kwargs["a"]),
        "success_" + suffix: success,
        "message_" + suffix: message,
        "loglike_ref": ts_diff.loglike_ref
    }


def confidence_scipy(parameters, parameter, function, sigma, **kwargs):
    with parameters.restore_values:
        result = _confidence_scipy_brentq(
            parameters=parameters,
            parameter=parameter,
            function=function,
            sigma=sigma,
            upper=False,
            **kwargs)

    with parameters.restore_values:
        result_errp = _confidence_scipy_brentq(
            parameters=parameters,
            parameter=parameter,
            function=function,
            sigma=sigma,
            upper=True,
            **kwargs)

    result.update(result_errp)
    return result


# TODO: implement, e.g. with numdifftools.Hessian
def covariance_scipy(parameters, function):
    raise NotImplementedError
