import pandas as pd
import numpy as np
import logging

from pyam.index import replace_index_values
from pyam.logging import adjust_log_level
from pyam.utils import (
    islistable,
    isstr,
    find_depth,
    reduce_hierarchy,
    KNOWN_FUNCS,
    to_list,
)

logger = logging.getLogger(__name__)


def _aggregate(df, variable, components=None, method=np.sum):
    """Internal implementation of the `aggregate` function"""

    # list of variables require default components (no manual list)
    if islistable(variable) and components is not None:
        raise ValueError(
            "Aggregating by list of variables does not support `components`!"
        )

    mapping = {}
    msg = "Cannot aggregate variable '{}' because it has no components!"
    # if single variable
    if isstr(variable):
        # default components to all variables one level below `variable`
        components = components or df._variable_components(variable)

        if not len(components):
            logger.info(msg.format(variable))
            return

        for c in components:
            mapping[c] = variable

    # else, use all variables one level below `variable` as components
    else:
        for v in variable if islistable(variable) else [variable]:
            _components = df._variable_components(v)
            if not len(_components):
                logger.info(msg.format(v))
                continue

            for c in _components:
                mapping[c] = v

    # rename all components to `variable` and aggregate
    _df = df._data[df._apply_filters(variable=mapping.keys())]
    _df.index = replace_index_values(_df, "variable", mapping)
    return _group_and_agg(_df, [], method)


def _aggregate_recursive(df, variable):
    """Recursive aggregation along the variable tree"""

    # downselect to components of `variable`
    df = df.filter(variable=f"{variable}|*")
    data_list = []

    # iterate over variables (bottom-up) and aggregate all components
    for d in reversed(range(1, max(find_depth(df.variable)) + 1)):
        vars = set([reduce_hierarchy(v, -1) for v in df.variable if find_depth(v) == d])
        _df = df.aggregate(variable=vars)
        df.append(_df, inplace=True)
        data_list.append(_df._data)

    return pd.concat(data_list)


def _aggregate_region(
    df, variable, region, subregions=None, components=False, method="sum", weight=None
):
    """Internal implementation for aggregating data over subregions"""
    if not isstr(variable) and components is not False:
        raise ValueError(
            "Aggregating by list of variables with components is not supported!"
        )

    if weight is not None and components is not False:
        raise ValueError("Using weights and components in one operation not supported!")

    # default subregions to all regions other than `region`
    subregions = subregions or df._all_other_regions(region, variable)

    if not len(subregions):
        logger.info(
            f"Cannot aggregate variable '{variable}' to '{region}' "
            "because it does not exist in any subregion!"
        )
        return

    # compute aggregate over all subregions
    subregion_df = df.filter(region=subregions)
    rows = subregion_df._apply_filters(variable=variable)
    if weight is None:
        _data = _group_and_agg(subregion_df._data[rows], "region", method=method)
    else:
        weight_rows = subregion_df._apply_filters(variable=weight)
        _data = _agg_weight(
            subregion_df._data[rows], subregion_df._data[weight_rows], method
        )

    # if not `components=False`, add components at the `region` level
    if components is not False:
        with adjust_log_level(logger):
            region_df = df.filter(region=region)

        # if `True`, auto-detect `components` at the `region` level,
        # defaults to variables below `variable` only present in `region`
        if components is True:
            level = dict(level=None)
            r_comps = region_df._variable_components(variable, **level)
            sr_comps = subregion_df._variable_components(variable, **level)
            components = set(r_comps).difference(sr_comps)

        if len(components):
            # rename all components to `variable` and aggregate
            rows = region_df._apply_filters(variable=components)
            _df = region_df._data[rows]
            mapping = dict([(c, variable) for c in components])
            _df.index = replace_index_values(_df.index, "variable", mapping)
            _data = _data.add(_group_and_agg(_df, "region"), fill_value=0)

    return _data


def _aggregate_time(df, variable, column, value, components, method=np.sum):
    """Internal implementation for aggregating data over subannual time"""
    # default `components` to all entries in `column` other than `value`
    if components is None:
        components = list(set(df.data.subannual.unique()) - set([value]))

    # compute aggregate over time
    filter_args = dict(variable=variable)
    filter_args[column] = components
    index = df._data.index.names.difference([column, "value"])

    _data = pd.concat(
        [
            df.filter(**filter_args)
            .data.pivot_table(index=index, columns=column)
            .value.rename_axis(None, axis=1)
            .apply(_get_method_func(method), axis=1)
        ],
        names=[column] + index,
        keys=[value],
    )

    # reset index-level order to original IamDataFrame
    _data.index = _data.index.reorder_levels(df._LONG_IDX)

    return _data


def _group_and_agg(df, by, method=np.sum):
    """Group-by & aggregate `pd.Series` by index names on `by`"""
    cols = [c for c in list(df.index.names) if c not in to_list(by)]
    # pick aggregator func (default: sum)
    return df.groupby(cols).agg(_get_method_func(method))


def _agg_weight(data, weight, method):
    """Aggregate `data` by regions with weights, return indexed `pd.Series`"""

    # only summation allowed with weights
    if method not in ["sum", np.sum]:
        raise ValueError("Only method 'np.sum' allowed for weighted average!")

    weight = weight.droplevel(["variable", "unit"])

    if not data.droplevel(["variable", "unit"]).index.equals(weight.index):
        raise ValueError("Inconsistent index between variable and weight!")

    col1 = data.index.names.difference(["region"])
    col2 = data.index.names.difference(["region", "variable", "unit"])
    return (data * weight).groupby(col1).sum() / weight.groupby(col2).sum()


def _get_method_func(method):
    """Translate a string to a known method"""
    if not isstr(method):
        return method

    if method in KNOWN_FUNCS:
        return KNOWN_FUNCS[method]

    # raise error if `method` is a string but not in dict of known methods
    raise ValueError(f"'{method}' is not a known method!")
