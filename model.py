import xarray as xr
import numpy as np
from agrifoodpy.food.food import FoodBalanceSheet
from agrifoodpy.utils.scaling import logistic_scale, linear_scale
import warnings
import copy

# from agrifoodpy.food.food_supply import scale_food, SSR
# from afp_config import *
# from helper_functions import *

def project_future(datablock, scale):
    """Project future food consumption based on scale
    
    Parameters
    ----------
    datablock : Dict
        Dictionary containing xarray datasets for population, food consumption etc.

    scale : xarray.DataArray
        Scale to apply to food consumption

    Returns
    -------
    datablock : Dict
        New dictionary containinng projected food consumption data
    """
    years = np.arange(2021,2101)

    # Per capita per day values remain constant
    g_cap_day = datablock["food"]["g/cap/day"]
    g_prot_cap_day = datablock["food"]["g_prot/cap/day"]
    g_fat_cap_day = datablock["food"]["g_fat/cap/day"]
    kcal_cap_day = datablock["food"]["kCal/cap/day"]

    years_past = g_cap_day.Year.values

    g_cap_day = g_cap_day.fbs.add_years(years, "constant")
    g_prot_cap_day = g_prot_cap_day.fbs.add_years(years, "constant")
    g_fat_cap_day = g_fat_cap_day.fbs.add_years(years, "constant")
    kcal_cap_day = kcal_cap_day.fbs.add_years(years, "constant")

    # Scale food production
    scale_past = xr.DataArray(np.ones(len(years_past)), dims=["Year"], coords={"Year": years_past})
    scale_tot = xr.concat([scale_past, scale], dim="Year")

    g_cap_day = g_cap_day.fbs.scale_add(element_in="production", element_out="imports", scale=1/scale_tot, add=False)
    g_prot_cap_day = g_prot_cap_day.fbs.scale_add(element_in="production", element_out="imports", scale=1/scale_tot, add=False)
    g_fat_cap_day = g_fat_cap_day.fbs.scale_add(element_in="production", element_out="imports", scale=1/scale_tot, add=False)
    kcal_cap_day = kcal_cap_day.fbs.scale_add(element_in="production", element_out="imports", scale=1/scale_tot, add=False)

    g_cap_day = g_cap_day.fbs.scale_add(element_in="exports", element_out="imports", scale=1/scale_tot)
    g_prot_cap_day = g_prot_cap_day.fbs.scale_add(element_in="exports", element_out="imports", scale=1/scale_tot)
    g_fat_cap_day = g_fat_cap_day.fbs.scale_add(element_in="exports", element_out="imports", scale=1/scale_tot)
    kcal_cap_day = kcal_cap_day.fbs.scale_add(element_in="exports", element_out="imports", scale=1/scale_tot)

    # Emissions per gram of food also remain constant
    g_co2e_g = datablock["impact"]["gco2e/gfood"]
    g_co2e_g = g_co2e_g.fbs.add_years(years, "constant")

    datablock["food"]["g/cap/day"] = g_cap_day
    datablock["food"]["g_prot/cap/day"] = g_prot_cap_day
    datablock["food"]["g_fat/cap/day"] = g_fat_cap_day
    datablock["food"]["kCal/cap/day"] = kcal_cap_day
    datablock["impact"]["gco2e/gfood"] = g_co2e_g

    return datablock

def item_scaling(datablock, scale, source, scaling_nutrient,
                 elasticity=None, items=None, item_group=None):
    """Reduces per capita daily ruminant meat intake and replaces its
    consumption by all other items keeping the overall food consumption constant
    """

    timescale = datablock["global_parameters"]["timescale"]
    # We can use any quantity here, either per cap/day or per year. The ratio
    # will cancel out the population growth
    food_orig = datablock["food"][scaling_nutrient]

    if np.isscalar(source):
        source = [source]
    
    # if no items are specified, do nothing
    if items is None and item_group is None:
        return datablock
    else:
        # if items are specified, select the items to scale
        # we prioritise items over item_origin
        if items is not None:
            pass
        # if item_origin is specified, select the items to scale
        elif item_group is not None:
            if np.isscalar(item_group):
                item_group = [item_group]
            items = food_orig.sel(Item = food_orig.Item_group.isin(item_group)).Item.values
            # items = food_orig.sel(Item = food_orig.Item_group==item_group).Item.values

    # Balanced scaling. Reduce food, reduce imports, keep kCal constant
    out = balanced_scaling(fbs=food_orig,
                           items=items,
                           element="food",
                           timescale=timescale,
                           year=2021,
                           scale=scale,
                           adoption="logistic",
                           origin=source,
                           add=True,
                           elasticity=elasticity,
                           constant=True,
                           fallback="exports",
                           add_fallback=False)

    # Scale feed, seed and processing
    out = feed_scale(out, food_orig)
    
    ratio = out / food_orig
    ratio = ratio.where(~np.isnan(ratio), 1)

    # Update per cap/day values and per year values using the same ratio, which
    # is independent of population growth
    qty_key = ["g/cap/day", "g_prot/cap/day", "g_fat/cap/day", "kCal/cap/day"]
    for key in qty_key:
        datablock["food"][key] *= ratio

    return datablock

def balanced_scaling(fbs, items, scale, element, year=None, adoption=None,
                     timescale=10, origin=None, add=True, elasticity=None,
                     constant=False, fallback=None, add_fallback=True):
    """Scale items quantities across multiple elements in a FoodBalanceSheet
    Dataset 
    
    Scales selected item quantities on a food balance sheet and with the
    posibility to keep the sum of selected elements constant.
    Optionally, produce an Dataset with a sequence of quantities over the years
    following a smooth scaling according to the selected functional form.

    The elements used to supply the modified quantities can be selected to keep
    a balanced food balance sheet.

    Parameters
    ----------
    fbs : xarray.Dataset
        Input food balance sheet Dataset.
    items : list
        List of items to scale in the food balance sheet.
    element : string
        Name of the DataArray to scale.
    scale : float
        Scaling parameter after full adoption.
    adoption : string, optional
        Shape of the scaling adoption curve. "logistic" uses a logistic model
        for a slow-fast-slow adoption. "linear" uses a constant slope adoption
        during the the "timescale period"
    year : int, optional
        Year of the Food Balance Sheet to use as pivot. If not set, the last
        year of the array is used
    timescale : int, optional
        Timescale for the scaling to be applied completely.  If "year" +
        "timescale" is greater than the last year in the array, it is extended
        to accomodate the extra years.
    origin : string, optional
        Name of the DataArray which will be used to balance the food balance
        sheets. Any change to the "element" DataArray will be reflected in this
        DataArray.
    add : bool, optional
        If set to True, the scaled element difference is added to the "origin"
        DataArray. If False, it is subtracted.
    elasticity : float, float array_like optional
        Fractional percentage of the difference that is added to each
        element in origin.
    constant : bool, optional
        If set to True, the sum of element remains constant by scaling the non
        selected items accordingly.
    fallback : string, optional
        Name of the DataArray used to provide the excess required to balance the
        food balance sheet in case the "origin" falls below zero.
    add_fallback : bool, optional
        If set to True, the excessis added to the fallback DataArray. If False,
        it is subtracted.

    Returns
    -------
    data : xarray.Dataarray
        Food balance sheet Dataset with scaled "food" values.
    """

    # Check for single item inputs
    if np.isscalar(items):
        items = [items]

    if np.isscalar(origin):
        origin = [origin]

    if np.isscalar(add):
        add = [add]*len(origin)

    # Check for single item list fbs
    input_item_list = fbs.Item.values
    if np.isscalar(input_item_list):
        input_item_list = [input_item_list]
        if constant:
            warnings.warn("Constant set to true but input only has a single item.")
            constant = False

    # If no items are provided, we scale all of them.
    if items is None or np.sort(items) is np.sort(input_item_list):
        items = fbs.Item.values
        if constant:
            warnings.warn("Cannot keep food constant when scaling all items.")
            constant = False

    # Define Dataarray to use as pivot
    if "Year" in fbs.dims:
        if year is None:
            if np.isscalar(fbs.Year.values):
                year = fbs.Year.values
                fbs_toscale = fbs
            else:
                year = fbs.Year.values[-1]
                fbs_toscale = fbs.isel(Year=-1)
        else:
            fbs_toscale = fbs.sel(Year=year)

    else:
        fbs_toscale = fbs
        try:
            year = fbs.Year.values
        except AttributeError:
            year=0

    # Define scale array based on year range
    if adoption is not None:
        if adoption == "linear":
            from agrifoodpy.utils.scaling import linear_scale as scale_func
        elif adoption == "logistic":
            from agrifoodpy.utils.scaling import logistic_scale as scale_func
        else:
            raise ValueError("Adoption must be one of 'linear' or 'logistic'")
        
        y0 = fbs.Year.values[0]
        y1 = year
        y2 = np.min([year + timescale, fbs.Year.values[-1]])
        y3 = fbs.Year.values[-1]
        
        scale_arr = scale_func(y0, y1, y2, y3, c_init=1, c_end = scale)
        
        # # Extend the dataset to include all the years of the array
        # fbs_toscale = fbs_toscale * xr.ones_like(scale_arr)
    
    else:
        scale_arr = scale    

    # Modify and return
    out = fbs.fbs.scale_add(element, origin, scale_arr, items, add=add,
                            elasticity=elasticity)    

    if constant:

        delta = out[element] - fbs[element]

        # Scale non selected items
        non_sel_items = np.setdiff1d(fbs.Item.values, items)
        non_sel_scale = (fbs.sel(Item=non_sel_items)[element].sum(dim="Item") - delta.sum(dim="Item")) / fbs.sel(Item=non_sel_items)[element].sum(dim="Item")
        
        # Make sure inf and nan values are not scaled
        non_sel_scale = non_sel_scale.where(np.isfinite(non_sel_scale)).fillna(1.0)

        if np.any(non_sel_scale < 0):
            warnings.warn("Additional consumption cannot be compensated by \
                        reduction of non-selected items")
        
        out = out.fbs.scale_add(element, origin, non_sel_scale, non_sel_items, add=add,
                            elasticity=elasticity)

        # If fallback is defined, adjust to prevent negative values
        if fallback is not None:
            df = sum(out[org].where(out[org] < 0).fillna(0) for org in origin)
            out[fallback] -= np.where(add_fallback, -1, 1)*df
            for org in origin:
                out[org] = out[org].where(out[org] > 0, 0)

    return out

def food_waste_model(datablock, waste_scale, kcal_rda, source):
    """Reduces daily per capita per day intake energy above a set threshold.
    """

    timescale = datablock["global_parameters"]["timescale"]
    food_orig = copy.deepcopy(datablock["food"]["kCal/cap/day"])
    datablock["food"]["rda_kcal"] = kcal_rda

    # This is the maximum factor we can multiply food by to achieve consumption
    # equal to rda_kcal, multiplied by the ambition level
    waste_factor = (food_orig["food"].isel(Year=-1).sum(dim="Item") - kcal_rda) \
                 / food_orig["food"].isel(Year=-1).sum(dim="Item") \
                 * (waste_scale / 100)
    
    waste_factor = waste_factor.to_numpy()

    # Create a logistic curve starting at 1, ending at 1-waste_factor
    scale_waste = logistic_food_supply(food_orig, timescale, 1, 1-waste_factor)

    # Set to "imports" or "production" to choose which element of the food system supplies the change in consumption
    # Scale food and subtract difference from production
    out = food_orig.fbs.scale_add(element_in="food",
                                  element_out=source,
                                  scale=scale_waste)
    
    # Scale feed, seed and processing
    # out = feed_scale(out, food_orig)

    # If supply element is negative, set to zero and add the negative delta to imports
    out = check_negative_source(out, source)

    # Scale all per capita qantities proportionally
    ratio = out / food_orig
    ratio = ratio.where(~np.isnan(ratio), 1)

    qty_key = ["g/cap/day", "g_prot/cap/day", "g_fat/cap/day", "kCal/cap/day"]
    for key in qty_key:
        datablock["food"][key] *= ratio

    return datablock

def cultured_meat_model(datablock, cultured_scale, labmeat_co2e, source, extra_items = []):
    """Replaces selected meat items by cultured products on a weight by weight
    basis. 
    """

    timescale = datablock["global_parameters"]["timescale"]
    items_to_replace = [2731, 2732]

    if len(extra_items) > 0:
        items_to_replace = np.concatenate([items_to_replace, extra_items])

    # Add cultured meat to the dataset
    qty_key = ["g/cap/day", "g_prot/cap/day", "g_fat/cap/day", "kCal/cap/day"]
    
    for key in qty_key:
        datablock["food"][key] = datablock["food"][key].fbs.add_items(5000)
        datablock["food"][key]["Item_name"].loc[{"Item":5000}] = "Cultured meat"
        datablock["food"][key]["Item_origin"].loc[{"Item":5000}] = "Cultured Products"
        datablock["food"][key]["Item_group"].loc[{"Item":5000}] = "Cultured Products"
        # Set values to zero to avoid issues
        datablock["food"][key].loc[{"Item":5000}] = 0

    # Scale products by cultured_scale
    food_orig = copy.deepcopy(datablock["food"]["g/cap/day"])

    scale_labmeat = logistic_food_supply(food_orig, timescale, 1, 1-cultured_scale)

    # Scale and remove from suplying element
    out = food_orig.fbs.scale_add(element_in="food",
                                  element_out=source,
                                  scale=scale_labmeat,
                                  items=items_to_replace,
                                  add=True)
    
    # If production is negative, set to zero and add the negative delta to
    # imports
    out = check_negative_source(out, source)
    
    # Add delta to cultured meat
    delta = (datablock["food"]["g/cap/day"]-out).sel(Item=items_to_replace).sum(dim="Item")
    out.loc[{"Item":5000}] += delta
    datablock["food"]["g/cap/day"] = out

    # Add nutrition values for cultured meat
    nutrition_keys = ["g_prot/g_food", "g_fat/g_food", "kCal/g_food"]
    for key in nutrition_keys:
        datablock["food"][key] = datablock["food"][key].fbs.add_items(5000, copy_from=[2731])
        datablock["food"][key]["Item_name"].loc[{"Item":5000}] = "Cultured meat"
        datablock["food"][key]["Item_origin"].loc[{"Item":5000}] = "Cultured Products"
        datablock["food"][key]["Item_group"].loc[{"Item":5000}] = "Cultured Products"

    # Add emissions factor for cultured meat
    datablock["impact"]["gco2e/gfood"] = datablock["impact"]["gco2e/gfood"].fbs.add_items(5000)
    datablock["impact"]["gco2e/gfood"].loc[{"Item":5000}] = labmeat_co2e

    # Recompute per capita values
    for key_pc, key_n in zip(qty_key[1:], nutrition_keys):
        datablock["food"][key_pc] = datablock["food"]["g/cap/day"] * datablock["food"][key_n]

    return datablock

def compute_emissions(datablock):
    """
    Computes the emissions per capita per day and per year for each food item,
    using the per capita daily weights and PN18 emissions factors.
    """
    pop = datablock["population"]["population"]
    pop_world = pop.sel(Region = 826)

    # Compute emissions per capita per day
    co2e_cap_day = datablock["food"]["g/cap/day"] * datablock["impact"]["gco2e/gfood"]

    # Compute emissions per year
    datablock["food"]["g_co2e/cap/day"] = co2e_cap_day
    datablock["impact"]["g_co2e/year"] = co2e_cap_day * pop_world * 365.25

    return datablock

def compute_t_anomaly(datablock):
    """Computes the temperature anomaly, concentration and radiation forcing from
    the per year emissions using the FAIR model.
    """

    from agrifoodpy.impact.model import fair_co2_only

    # g co2e / year
    g_co2e_year = datablock["impact"]["g_co2e/year"]["production"].sum(
        dim="Item")

    # Gt co2e / year
    Gt_co2e_year = g_co2e_year * 1e-15

    # Compute temperature anomaly based on emissions
    T, C, F = fair_co2_only(Gt_co2e_year)

    T = T.rename({"timebounds": "Year"})
        
    datablock["impact"]["T"] = T
    datablock["impact"]["C"] = C
    datablock["impact"]["F"] = F

    return datablock

def spare_alc_model(datablock, spare_fraction, land_type, items, alc_grades=None):
    """Replaces a specified land type fraction and sets it to a new type called
    'spared'. Scales food production and imports to reflect the change in land
    use.
    """
    
    timescale = datablock["global_parameters"]["timescale"]
    alc = datablock["land"]["dominant_classification"]
    pctg = datablock["land"]["percentage_land_use"].copy(deep=True)
    old_use = datablock["land"]["percentage_land_use"].sel({"aggregate_class":land_type}).sum()

    # if no alc grade is provided, then use the whole map
    if alc_grades is not None:
        alc_mask = np.isin(alc, alc_grades)
    else:
        alc_mask = np.ones_like(pctg, dtype=bool)

    to_spare = pctg.where(alc_mask, other=0).sel({"aggregate_class":land_type})

    # Spare the specified land type
    delta_spared =  to_spare * spare_fraction
    pctg.loc[{"aggregate_class":land_type}] -= delta_spared

    if "Spared" not in pctg.aggregate_class.values:
        spared_new_class = xr.zeros_like(pctg.isel(aggregate_class=0)).where(np.isfinite(pctg.isel(aggregate_class=0)))
        spared_new_class["aggregate_class"] = "Spared"
        pctg = xr.concat([pctg, spared_new_class], dim="aggregate_class")

    pctg.loc[{"aggregate_class":"Spared"}] += delta_spared.sum(dim="aggregate_class")

    # Add spared class to the land use map
    datablock["land"]["percentage_land_use"] = pctg

    # Scale food production and imports
    new_use = pctg.sel({"aggregate_class":land_type}).sum()
    scale_use = (new_use/old_use).to_numpy()

    food_orig = datablock["food"]["g/cap/day"]
    scale_spare = logistic_food_supply(food_orig, timescale, 1, scale_use)

    scaled_items = food_orig.sel(Item=food_orig.Item_origin==items).Item.values

    out = food_orig.fbs.scale_add(element_in="production",
                                  element_out="imports",
                                  scale=scale_spare,
                                  items=scaled_items,
                                  add=False)
    
    ratio = out / food_orig
    ratio = ratio.where(~np.isnan(ratio), 1)

    # Update per cap/day values and per year values using the same ratio, which
    # is independent of population growth
    qty_key = ["g/cap/day", "g_prot/cap/day", "g_fat/cap/day", "kCal/cap/day"]
    for key in qty_key:
        datablock["food"][key] *= ratio

    # datablock["food"]["g/cap/day"] = out

    return datablock

def foresting_spared_model(datablock, forest_fraction, bdleaf_conif_ratio):
    """Replaces a the "spared" land type fraction and sets it to "forested".
    """

    # Load land use data from datablock
    pctg = datablock["land"]["percentage_land_use"].copy(deep=True)

    # Compute spared fraction to be re forested and remove from the spared class
    delta_spared = pctg.loc[{"aggregate_class":"Spared"}] * forest_fraction
    pctg.loc[{"aggregate_class":"Spared"}] -= delta_spared

    # Add forested percentage to the land use map
    pctg.loc[{"aggregate_class":"Broadleaf woodland"}] += delta_spared * bdleaf_conif_ratio
    pctg.loc[{"aggregate_class":"Coniferous woodland"}] += delta_spared * (1-bdleaf_conif_ratio)

    # Rewrite land use data to datablock
    datablock["land"]["percentage_land_use"] = pctg

    return datablock

def ccs_model(datablock, waste_BECCS, overseas_BECCS, DACCS):
    """Computes the CCS sequestration from the different sources
    
    Parameters
    ----------

    waste_BECCS : float
        Total maximum sequestration (in t CO2e / year) from food waste-origin BECCS
    overseas_BECCS : float
        Total maximum sequestration (in t CO2e / year) from overseas biomass BECCS
    DACCS : float
        Total maximum sequestration (in t CO2e / year) from DACCS
    """
    
    timescale = datablock["global_parameters"]["timescale"]
    food_orig = datablock["food"]["g/cap/day"]
    pctg = datablock["land"]["percentage_land_use"]

    # Compute the total area of BECCS land used in hectares, and the total
    # sequestration in Mt CO2e / year

    land_BECCS_area = pctg.sel({"aggregate_class":"BECCS"}).sum().to_numpy()
    land_BECCS = land_BECCS_area * 23.5

    logistic_0_val = logistic_food_supply(food_orig, timescale, 0, 1)

    waste_BECCS_seq_array = waste_BECCS * logistic_0_val
    overseas_BECCS_seq_array = overseas_BECCS * logistic_0_val
    DACCS_seq_array = DACCS * logistic_0_val
    land_BECCS_seq_array = land_BECCS * logistic_0_val

    # Create a dataset with the different sequestration sources
    seq_ds = xr.Dataset({"BECCS from waste": waste_BECCS_seq_array,
                         "BECCS from overseas biomass": overseas_BECCS_seq_array,
                         "BECCS from land": land_BECCS_seq_array,
                         "DACCS": DACCS_seq_array})
    
    seq_da = seq_ds.to_array(dim="Item", name="sequestration")
    
    if "co2e_sequestration" not in datablock["impact"]:
        datablock["impact"]["co2e_sequestration"] = seq_da
    else:
        # append sequestration to existing sequestration da
        seq_da_in = datablock["impact"]["co2e_sequestration"]
        seq_da = xr.concat([seq_da_in, seq_da], dim="Item")
        datablock["impact"]["co2e_sequestration"] = seq_da

    # Compute the total cost of sequestration in pounds per year
    cost_BECCS_tCO2e = linear_scale(food_orig.Year.values[0],
                              2030,
                              2050,
                              food_orig.Year.values[-1],
                              c_init=123,
                              c_end=93)
    
    cost_DACCS_tCO2e = linear_scale(food_orig.Year.values[0],
                                    2030,
                                    2050,
                                    food_orig.Year.values[-1],
                                    c_init=245,
                                    c_end=180)

    cost_waste_BECCS = waste_BECCS_seq_array * cost_BECCS_tCO2e
    cost_overseas_BECCS = overseas_BECCS_seq_array * cost_BECCS_tCO2e
    cost_land_BECCS = land_BECCS_seq_array * cost_BECCS_tCO2e
    cost_DACCSS = DACCS_seq_array * cost_DACCS_tCO2e

    cost_CCS_ds = xr.Dataset({"BECCS from waste": cost_waste_BECCS,
                             "BECCS from overseas biomass": cost_overseas_BECCS,
                             "BECCS from land": cost_land_BECCS,
                             "DACCS": cost_DACCSS})
    
    cost_CCS_da = cost_CCS_ds.to_array(dim="Item", name="cost")
    datablock["impact"]["cost"] = cost_CCS_da

    return datablock    

def forest_sequestration_model(datablock, seq_broadleaf_ha_yr, seq_coniferous_ha_yr):
    """Computes total annual sequestration from the different sources"""
    
    timescale = datablock["global_parameters"]["timescale"]
    food_orig = datablock["food"]["g/cap/day"]

    # Load the land use data from the datablock
    pctg = datablock["land"]["percentage_land_use"].copy(deep=True)

    # Compute forest area in ha, maximum anual sequestration, and growth curve
    area_broadleaf = pctg.loc[{"aggregate_class":"Broadleaf woodland"}].sum().to_numpy()
    area_coniferous = pctg.loc[{"aggregate_class":"Coniferous woodland"}].sum().to_numpy()

    max_seq_broadleaf = area_broadleaf * seq_broadleaf_ha_yr
    max_seq_coniferous = area_coniferous * seq_coniferous_ha_yr

    logistic_0_val = logistic_food_supply(food_orig, timescale, 0, 1)
    
    broadleaf_seq = max_seq_broadleaf * logistic_0_val
    coniferous_seq = max_seq_coniferous * logistic_0_val

    # Create a dataset with the different sequestration sources
    seq_ds = xr.Dataset({"Broadleaved woodland": broadleaf_seq,
                              "Coniferous woodland": coniferous_seq})
    
    seq_da = seq_ds.to_array(dim="Item", name="sequestration")
    
    if "co2e_sequestration" not in datablock["impact"]:
        datablock["impact"]["co2e_sequestration"] = seq_da
    else:
        # append sequestration to existing sequestration da
        seq_da_in = datablock["impact"]["co2e_sequestration"]
        seq_da = xr.concat([seq_da_in, seq_da], dim="Item")
        datablock["impact"]["co2e_sequestration"] = seq_da

    # Compute agroecology sequestration

    return datablock

def scale_impact(datablock, scale_factor, item_origin=None, items=None):
    """ Scales the impact values for the selected items by multiplying them by
    a multiplicative factor.
    """

    timescale = datablock["global_parameters"]["timescale"]
    # load quantities and impacts
    food_orig = datablock["food"]["g/cap/day"]
    impacts = datablock["impact"]["gco2e/gfood"].copy(deep=True)

    # if no items are specified, do nothing
    if items is None and item_origin is None:
        return datablock
    else:
        # if items are specified, select the items to scale
        # we prioritise items over item_origin
        if items is not None:
            pass
        # if item_origin is specified, select the items to scale
        elif item_origin is not None:
            items = food_orig.sel(Item = food_orig.Item_origin==item_origin).Item.values
            items = items[np.isin(items, impacts.Item.values)]
    
    scale = logistic_food_supply(food_orig, timescale, 1, scale_factor)

    # scale the impacts
    impacts.loc[{"Item": items}] *= scale
    datablock["impact"]["gco2e/gfood"] = impacts

    return datablock

def scale_production(datablock, scale_factor, item_origin=None, items=None):
    """ Scales the production values for the selected items by multiplying them by
    a multiplicative factor.
    """

    timescale = datablock["global_parameters"]["timescale"]

    # load quantities and impacts
    food_orig = datablock["food"]["g/cap/day"].copy(deep=True)

    # if no items are specified, do nothing
    if items is None and item_origin is None:
        return datablock
    else:
        # if items are specified, select the items to scale
        # we prioritise items over item_origin
        if items is not None:
            pass
        # if item_origin is specified, select the items to scale
        elif item_origin is not None:
            items = food_orig.sel(Item = food_orig.Item_origin==item_origin).Item.values

    scale_prod = logistic_food_supply(food_orig, timescale, 1, scale_factor)

    out = food_orig.fbs.scale_add(element_in="production",
                                element_out="imports",
                                scale=scale_prod,
                                items=items,
                                add=False)
    
    ratio = out / food_orig
    ratio = ratio.where(~np.isnan(ratio), 1)

    # Update per cap/day values and per year values using the same ratio, which
    # is independent of population growth
    qty_key = ["g/cap/day", "g_prot/cap/day", "g_fat/cap/day", "kCal/cap/day"]
    for key in qty_key:
        datablock["food"][key] *= ratio

    return datablock

def BECCS_farm_land(datablock, farm_percentage):
    """Repurposes farm land for BECCS, reducing the amount of food production,
    and increasing the amount of CO2e sequestered.
    """

    timescale = datablock["global_parameters"]["timescale"]
    pctg = datablock["land"]["percentage_land_use"].copy(deep=True)
    old_use = datablock["land"]["percentage_land_use"].sel({"aggregate_class":"Arable"}).sum()
    
    to_spare = pctg.sel({"aggregate_class":"Arable"})
    # Spare the specified land type
    delta_spared =  to_spare * farm_percentage
    pctg.loc[{"aggregate_class":"Arable"}] -= delta_spared

    if "BECCS" not in pctg.aggregate_class.values:
        spared_new_class = xr.zeros_like(pctg.isel(aggregate_class=0)).where(np.isfinite(pctg.isel(aggregate_class=0)))
        spared_new_class["aggregate_class"] = "BECCS"
        pctg = xr.concat([pctg, spared_new_class], dim="aggregate_class")

    pctg.loc[{"aggregate_class":"BECCS"}] += delta_spared

    # Add spared class to the land use map
    datablock["land"]["percentage_land_use"] = pctg

    # Scale food production and imports
    new_use = pctg.sel({"aggregate_class":"Arable"}).sum()
    scale_use = (new_use/old_use).fillna(1).to_numpy()

    food_orig = datablock["food"]["g/cap/day"]
    scale_spare = logistic_food_supply(food_orig, timescale, 1, scale_use)

    scaled_items = food_orig.sel(Item=food_orig.Item_origin=="Vegetal Products").Item.values

    out = food_orig.fbs.scale_add(element_in="production",
                                  element_out="imports",
                                  scale=scale_spare,
                                  items=scaled_items,
                                  add=False)
    
    ratio = out / food_orig
    ratio = ratio.where(~np.isnan(ratio), 1)

    # Update per cap/day values and per year values using the same ratio, which
    # is independent of population growth
    qty_key = ["g/cap/day", "g_prot/cap/day", "g_fat/cap/day", "kCal/cap/day"]
    for key in qty_key:
        datablock["food"][key] *= ratio

    return datablock

def agroecology_model(datablock, land_percentage, land_type, 
                      agroecology_class="Agroecology", tree_coverage=0.1,
                      replaced_items=None, new_items=None, item_yield=None,
                      seq_ha_yr=6.26):
    """Changes traditional agricultural land use to agroecological land use.
    
    Parameters
    ----------
    datablock : dict
        The datablock dictionary, containing all the model parameters and
        datasets.
    land_type : list
        The type or types of land that will be converted to agroecology.
    land_percentage : list
        The percentage or percentages of land that will be converted to
        agroecology.
    tree_coverage : float
        The percentage of each land class that will be converted to trees. This
        also sets the production value of the land class, via a 1-tree_coverage
        factor.
    replaced_items : list
        The items that will be replaced by agroecological products.
    new_items : list
        The additional items that will be grown in agroecological land.
    item_yield : float
        The yield of the additional agroecological products in g/ha/day.
    seq_ha_yr : float
        CO2e sequestration of agroecological land in t CO2e/ha/year.

    Returns
    -------
    datablock : dict
        The updated datablock dictionary, containing all the model parameters
        and datasets.
    """

    # Load land use and food data from datablock
    pctg = datablock["land"]["percentage_land_use"].copy(deep=True)
    food_orig = datablock["food"]["g/cap/day"].copy(deep=True)
    old_use = pctg.sel({"aggregate_class":land_type}).sum()
    alc = datablock["land"]["dominant_classification"]
    timescale = datablock["global_parameters"]["timescale"]

    # Compute land percentages to be converted to agroecology and remove them
    # from the land_type classes
    delta_agroecology = pctg.loc[{"aggregate_class":land_type}] * land_percentage
    pctg.loc[{"aggregate_class":land_type}] -= delta_agroecology

    # Add the agroecology percentage to the new agroecology class
    if agroecology_class not in pctg.aggregate_class.values:
        new_class = xr.zeros_like(pctg.isel(aggregate_class=0)).where(np.isfinite(pctg.isel(aggregate_class=0)))
        new_class["aggregate_class"] = agroecology_class
        pctg = xr.concat([pctg, new_class], dim="aggregate_class")

    delta_total = delta_agroecology.sum(dim="aggregate_class")
    pctg.loc[{"aggregate_class":agroecology_class}] += delta_total

    out = food_orig.copy(deep=True)

    # Reduce production of replaced items if they are provided
    if replaced_items is not None:
        new_use = pctg.sel({"aggregate_class":land_type}).sum()
        scale_use = (new_use/old_use) + (1-tree_coverage) * (1-new_use/old_use)
        scale_use = scale_use.to_numpy()

        scale_arr = logistic_food_supply(out, timescale, 1, scale_use)

        out = out.fbs.scale_add(element_in="production",
                                element_out="imports",
                                scale=scale_arr,
                                items=replaced_items,
                                add=False)

    # Add new items by scaling production from current values to future values
    if new_items is not None:
        if np.isscalar(new_items):
            new_items = [new_items]
        if np.isscalar(item_yield):
            item_yield = [item_yield]

        pop = datablock["population"]["population"].isel(Year=-1, Region=0)

        for item, yld in zip(new_items, item_yield):
            old_production = food_orig["production"].sel({"Item":item}).isel(Year=-1)
            new_production = old_production + yld * delta_agroecology.sum()/pop
            production_scale = (new_production / old_production).to_numpy()
            production_scale_array = logistic_food_supply(food_orig, timescale, 1, production_scale)

            out = out.fbs.scale_add(element_in="production",
                                element_out="imports",
                                scale=production_scale_array,
                                items=item,
                                add=False)
        
    # Compute forest area in ha, maximum anual sequestration, and growth curve
    area_agroecology = pctg.loc[{"aggregate_class":agroecology_class}].sum().to_numpy()
    max_seq_agroecology = area_agroecology * seq_ha_yr

    agroecology_seq = logistic_food_supply(food_orig, timescale, 1, c_end=max_seq_agroecology)
    
    # Create a dataset with the different sequestration sources
    seq_ds = xr.Dataset({agroecology_class: agroecology_seq})
    
    seq_da = seq_ds.to_array(dim="Item", name="sequestration")
    if "co2e_sequestration" not in datablock["impact"]:
        datablock["impact"]["co2e_sequestration"] = seq_da

    else:
        # append sequestration to existing sequestration da
        seq_da_in = datablock["impact"]["co2e_sequestration"]
        seq_da = xr.concat([seq_da_in, seq_da], dim="Item")
        datablock["impact"]["co2e_sequestration"] = seq_da

    # Rewrite land use data to datablock
    datablock["land"]["percentage_land_use"] = pctg

    ratio = out / food_orig
    ratio = ratio.where(~np.isnan(ratio), 1)

    # Update per cap/day values and per year values using the same ratio, which
    # is independent of population growth
    qty_key = ["g/cap/day", "g_prot/cap/day", "g_fat/cap/day", "kCal/cap/day"]
    for key in qty_key:
        datablock["food"][key] *= ratio

    return datablock

def feed_scale(fbs, ref):
    """Scales the feed, seed and processing quantities according to the change
    in production of animal and vegetal products"""

    # Obtain reference production values
    ref_feed_arr = ref["production"].sel(Item=ref.Item_origin=="Animal Products").sum(dim="Item")
    ref_seed_arr = ref["production"].sel(Item=ref.Item_origin=="Vegetal Products").sum(dim="Item")
    
    # Compute scaling factors for feed and seed based on proportional production
    feed_scale = fbs["production"].sel(Item=fbs.Item_origin=="Animal Products").sum(dim="Item") \
                / ref_feed_arr
    seed_scale = fbs["production"].sel(Item=fbs.Item_origin=="Vegetal Products").sum(dim="Item") \
                / ref_seed_arr

    # Set feed_scale and seed_scale to 1 where ref arrays are close or equal to zero
    feed_scale = xr.where(np.isclose(ref_feed_arr, 0), 1, feed_scale)
    feed_scale = xr.where(np.isclose(ref_seed_arr, 0), 1, seed_scale)

    processing_scale = fbs["production"].sum(dim="Item") \
                / ref["production"].sum(dim="Item")

    out = fbs.fbs.scale_add(element_in="feed", element_out="production",
                            scale=feed_scale)
    
    out = out.fbs.scale_add(element_in="seed",element_out="production",
                            scale=seed_scale)
    
    out = out.fbs.scale_add(element_in="processing",element_out="production",
                            scale=processing_scale)
    
    return out

def check_negative_source(fbs, source):
    """Checks for negative values in the source element and adds the difference
    to the fallback element"""

    if source == "production":
        fallback = "imports"
    elif source == "imports":
        fallback = "production"
    elif source == "exports":
        fallback = "production"

    delta_neg = fbs[source].where(fbs[source] < 0, other=0)
    fbs[source] -= delta_neg
    fbs[fallback] += delta_neg

    return fbs

def logistic_food_supply(fbs, timescale, c_init, c_end):
    """Creates a logistic curve using the year range of the input food balance
    supply"""

    y0 = fbs.Year.values[0]
    y1 = 2021
    y2 = 2021 + timescale
    y3 = fbs.Year.values[-1]

    scale = logistic_scale(y0, y1, y2, y3, c_init=c_init, c_end=c_end)

    return scale