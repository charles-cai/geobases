#!/usr/bin/python
# -*- coding: utf-8 -*-

'''
This module is a general class *GeoBase* to manipulate geographical
data. It loads static csv files containing data about
airports or train stations, and then provides tools to browse it.


It relies on two other modules:

    - Geoutils: to compute haversine distances between points
    - LevenshteinUtils: to calculate distances between strings. Indeed, we need
      a good tool to do it, in order to recognize things like station names
      in schedule files where we do not have the station id


Examples for airports::

    >>> geo_a = GeoBase(data='airports', verbose=False)
    >>> geo_a.findNearKey('ORY', 50) # Orly, airports <= 50km
    [(0.0, 'ORY'), (18.8..., 'TNF'), (27.8..., 'LBG'), (34.8..., 'CDG')]
    >>> geo_a.get('CDG', 'city_code')
    'PAR'
    >>> geo_a.haversine('CDG', 'NCE')
    694.5162...


Examples for stations::

    >>> geo_t = GeoBase(data='stations', verbose=False)
    >>>
    >>> # Nice, stations <= 5km
    >>> [geo_t.get(k, 'name') for d, k in geo_t.findNearPoint(43.70, 7.26, 5)]
    ['Nice-Ville', 'Nice-Riquier', 'Nice-St-Roch', 'Villefranche-sur-Mer', 'Nice-St-Augustin']
    >>>
    >>> geo_t.get('frpaz', 'name')
    'Paris-Austerlitz'
    >>> geo_t.haversine('frnic', 'frpaz')
    683.526...

'''

from __future__ import with_statement

import heapq
import numpy as np
from datetime import datetime
from itertools import groupby

try:
    import simplejson as json
except ImportError:
    import json

from SysUtils         import localToFile
from GeoUtils         import haversine
from LevenshteinUtils import mod_leven, clean


class GeoBase(object):
    '''
    This is the main and only class. After __init__,
    a file is loaded in memory, and the user may use
    the instance to get information.
    '''

    def __init__(self, data, source=None, verbose=True):
        '''Initialization

        :param data: the type of data wanted, 'airports', 'stations' \
            and 'mix' currently available. 'mix' will not load anything, \
            it is a way to get an empty object, which wille be fed later
        :param verbose: display informations or not during runtime

        :raises: ValueError, if data parameters is not recognized

        >>> geo_a = GeoBase(data='airports')
        Import successful from ...
        Available info for things: ...
        >>> geo_t = GeoBase(data='stations')
        Import successful from ...
        Available info for things: ...
        >>> geo_m = GeoBase(data='mix')
        >>>
        >>> GeoBase(data='odd')
        Traceback (most recent call last):
        ValueError: Wrong data type. Not in ['airports', 'airports_csv', 'countries', 'stations', 'stations_nls', 'mix']
        '''

        # Main structure in which everything will be loaded
        # Dictionary of dictionary
        self._things = {}

        # A cache for the fuzzy searches
        self._cache_fuzzy = {}
        # An other cache if the algorithms are failing on a single
        # example, we first look in this cache
        self._bias_cache_fuzzy = {}

        # For aggregation function
        self._date_formats = [
            'year',
            'month',
            'day',
            'month_of_year',
            'day_of_week',
            'day_of_month'
        ]
        self._default_date_format = "%Y%m%d"

        if data == 'airports':
            if source is None:
                source = localToFile(__file__, "DataSources/Airports/airports_geobase.csv")

            self._delimiter = '^'
            self._headers = [
                'code',
                'name',
                'city_code',
                'country_code',
                'country_name',
                'lat',
                'lng'
            ]

            self._loadFile(source, verbose=verbose)

            for thing in self._things:
                self.set(thing, 'type', 'airport')


        elif data == 'airports_csv':
            if source is None:
                source = localToFile(__file__, "DataSources/Airports/AirportsDotCsv/ORI_Simple_Airports_Database_Table.csv")

            self._delimiter = '^'
            self._headers = [
                'code',
                'ref_name',
                'ref_name_2',
                'name',
                'city_code',
                'is_an_airport',
                'state_code',
                'country_code',
                'region_code',      # REL_REGION_CODE in Amadeus RFD: AFRIC,CARIB,EUROP,PACIF,SAMER,...
                'pricing_zone',     # REL_CONTINENT_CODE in Amadeus RFD: ITC1, ITC2, ITC3
                'time_zone_group',  # REL_TIME_ZONE_GRP
                'lng',
                'lat',
                None,               # numeric code?
                'is_commercial',
                'location_type'     # C city, A airport, H heliport, O train station, R rail
            ]

            self._loadFile(source, verbose=verbose)

            # In fact here we have more than airports (cities, stations...)
            #for thing in self._things:
            #    self.set(thing, 'type', 'airport')


        elif data == 'countries':
            if source is None:
                source = localToFile(__file__, "DataSources/Countries/list_countries.csv")

            self._delimiter = '^'
            self._headers = [
                'name',
                'code'
            ]

            self._loadFile(source, key_col=1, verbose=verbose)

            for thing in self._things:
                self.set(thing, 'type', 'country')


        elif data == 'stations':
            if source is None:
                source = localToFile(__file__, "DataSources/TrainStations/stations_geobase.csv")

            self._delimiter = '^'
            self._headers = [
                'code',
                'lines',
                'name',
                'info',
                'lat',
                'lng'
            ]

            self._loadFile(source, verbose=verbose)

            for thing in self._things:
                self.set(thing, 'type', 'station')


        elif data == 'stations_nls':
            if source is None:
                source = localToFile(__file__, "DataSources/TrainStations/NLS/NLS CODES RefDataSNCF.csv")

            self._delimiter = ','
            self._headers = [
                'uic_code',
                'name',
                'code',
                'physical'
            ]

            self._loadFile(source, key_col=2, verbose=verbose)

            for thing in self._things:
                self.set(thing, 'type', 'station')


        elif data == 'mix':
            if source is None:
                source = None

            self._delimiter = '^' # useless, no load for mix type
            self._headers = [
                'code',
                'name',
                'lat',
                'lng'
            ]

        else:
            raise ValueError('Wrong data type. Not in %s' % ['airports', 'airports_csv', 'countries', 'stations', 'stations_nls', 'mix'])



    def _loadFile(self, source, key_col=0, verbose=True):
        '''Load the file and feed the self._things.

        :param source: the path to the source file
        :param verbose: display informations or not during runtime
        :raises: IOError, if the source cannot be read
        :raises: ValueError, if duplicates are found in the source
        '''

        with open(source) as f:

            for row in f:
                # Skip comments and empty lines
                if not row or row.startswith('#'):
                    continue

                row = row.strip().split(self._delimiter)

                # No duplicates ever
                if row[key_col] in self._things:
                    print "/!\ %s already in base: %s" % (row[key_col], str(self._things[row[key_col]]))

                #self._headers represents the meaning of each column.
                self._things[row[key_col]] = dict(
                    (self._headers[i], row[i])
                    for i in xrange(len(self._headers))
                    if self._headers[i] is not None
                )

            # We remove None headers, which are not-loaded-columns
            self._headers = [h for h in self._headers if h is not None]

        if verbose:
            print "Import successful from %s" % source
            print "Available info for things: %s" % self._headers


    def changeDateFormat(self, new_date_format):
        '''
        Change default date format for aggregation function.
        '''
        self._default_date_format = new_date_format


    def _convertDate(self, date, field):
        '''
        Method to convert dates, used for aggregation algorithms.

        >>> geo_a._convertDate("20101026", "month")
        '201010'
        >>> geo_a._convertDate("20101026", "day_of_week")
        'Tuesday'
        >>> geo_a.changeDateFormat("%Y/%m/%d")
        >>> geo_a._convertDate("2010/10/26", "day_of_week")
        'Tuesday'
        >>> geo_a.changeDateFormat("%Y%m%d")
        '''

        dt = datetime.strptime(date, self._default_date_format)

        if   field == 'year'         : return dt.strftime("%Y")
        elif field == 'month'        : return dt.strftime("%Y%m")
        elif field == 'day'          : return dt.strftime("%Y%m%d")
        elif field == 'day_of_week'  : return dt.strftime("%A")
        elif field == 'day_of_month' : return dt.strftime("%d")
        elif field == 'month_of_year': return dt.strftime("%B")
        else:
            raise ValueError("Bad date format, not in %s" % self._date_formats)


    def get(self, key, field=None):
        '''
        Simple get on the database.
        This get function raise exception when input is not correct.

        :param key:   the key of the thing (like 'SFO')
        :param field: the field (like 'name' or 'lat')
        :raises:      KeyError, if the key is not in the base
        :returns:     the needed information

        >>> geo_a.get('CDG', 'city_code')
        'PAR'
        >>> geo_t.get('frnic', 'name')
        'Nice-Ville'
        >>>
        >>> geo_t.get('frnic', 'not_a_field')
        Traceback (most recent call last):
        KeyError: "Field not_a_field not in ['code', 'lines', 'name', 'info', 'lat', 'lng', 'type']"
        >>> geo_t.get('frmoron', 'name')
        Traceback (most recent call last):
        KeyError: 'Thing not found: frmoron'
        '''

        try:
            if field is None:
                res = self._things[key]
            else:
                res = self._things[key][field]

        except KeyError:

            if key not in self._things:
                raise KeyError("Thing not found: %s" % str(key))

            raise KeyError("Field %s not in %s" % (field, self._headers))

        else:
            return res


    def _extGet(self, key, field=None):
        '''
        Same as get, but with useful addition for aggregation.

        :param key:     the key of the thing (like 'SFO')
        :param field:   the field (like 'name' or 'lat')
        :param verbose: display information of potential catched exceptions
        :returns:       the needed information

        >>> geo_a._extGet('CDG', 'city_code')
        'PAR'
        >>> geo_t._extGet('frnic', 'name')
        'Nice-Ville'
        >>> geo_t._extGet('frnic', '*')
        '*'
        >>> geo_t._extGet('frnic', None)
        'frnic'

        This get function raise exception when input is not correct.

        >>> geo_t._extGet('frnic', 'not_a_field')
        Traceback (most recent call last):
        ValueError: Bad field not_a_field not in ['code', 'lines', 'name', 'info', 'lat', 'lng', 'type'] or ['year', 'month', 'day', 'month_of_year', 'day_of_week', 'day_of_month']
        >>> geo_t._extGet('frmoron', 'name')
        Traceback (most recent call last):
        KeyError: 'Thing not found: frmoron'
        '''

        if field is None:
            return key

        if field == '*':
            return '*'

        if field in self._headers:
            return self.get(key, field)

        if field in self._date_formats:
            return self._convertDate(key, field)

        raise ValueError("Bad field %s not in %s or %s" % (field, self._headers, self._date_formats))


    def multiGet(self, T_key, T_field):
        '''
        Almost the same as get: method to get an information-tuple
        from an thing-tuple. Not very useful, but its a cool wrapper
        when you perform data aggregation.

        :param T_key:   a tuple of keys, like ('ORY', 'SFO')
        :param T_field: a tuple of fields, like ('name', 'lat')
        :returns:       a tuple of informations
        :raises:        IndexError, if the two input tuples have different size.

        >>> geo_a.multiGet(('ORY', 'CDG'), ('city_code', 'country_name'))
        ('PAR', 'France')
        >>> geo_t.multiGet(('frnic', 'frxat', 'frpmo'), ('*', None, 'name'))
        ('*', 'frxat', 'Paris-Montparnasse')
        >>> geo_a.multiGet(('ORY', ), ('city_code', ))
        ('PAR',)
        >>> geo_a.multiGet('ORY', None)
        'ORY'
        '''

        # Ok we manage moronic user input :), but really
        # the spirit is to give tuples
        if not isinstance(T_field, tuple):
            return self._extGet(T_key, T_field)

        # Loop in the tuple
        return tuple(self._extGet(k, f) for k, f in zip(T_key, T_field))



    def aggregation(self, dict_data, T_field, op='+', safe_mode=False, quiet=False):
        '''
        The aggregation function.

        >>> test = {'NCE':15, 'ORY':20, 'AFW':50, 'CDG':27, 'DFW':5}
        >>> geo_a.aggregation(test, 'country_code')
        {'FR': 62, 'US': 55}

        >>> test = { ('ORY', 'CDG', '20061101'): 4,
        ...          ('ORY', 'NCE', '20061108'): 5,
        ...          ('AAA', 'NCE', '20061201'): 5 }
        >>> geo_a.aggregation(test,
        ...                   ('*', 'city_code', 'year'),
        ...                   op=('+', 'mean', 'keep'))
        {('*', 'NCE', '2006'): (10, 5.0, [5, 5]), ('*', 'PAR', '2006'): (4, 4.0, [4])}

        >>> geo_a.aggregation(test,
        ...                   ('country_code', 'country_code', 'day_of_week'),
        ...                   int.__add__)
        {('FR', 'FR', 'Wednesday'): 9, ('PF', 'FR', 'Friday'): 5}
        >>> geo_a.aggregation(test,
        ...                   ('*', '*', 'day_of_week'),
        ...                   'mean')
        {('*', '*', 'Friday'): 5.0, ('*', '*', 'Wednesday'): 4.5}

        >>> test = {'NCE':15, 'ORY':20, 'AFW':50, 'CDG':27, 'DFW':5, 'UNKNOWN_AIRPORT': 150}
        >>> geo_a.aggregation(test,
        ...                   'country_code')
        Traceback (most recent call last):
        KeyError: 'Thing not found: UNKNOWN_AIRPORT'
        >>> geo_a.aggregation(test,
        ...                     'country_code',
        ...                     safe_mode=True)
        /!\ Some keys were not found /!\: ['UNKNOWN_AIRPORT']
        {'FR': 62, 'US': 55}
        >>> geo_a.aggregation(test,
        ...                   'country_code',
        ...                   safe_mode=True,
        ...                   quiet=True)
        {'FR': 62, 'US': 55}

        :param dict_data: is an input dictionary of airports, \
            values should be data implementing __add__ method, like int \
            and keys are tuple as ('NCE', '2006')
        :param T_field: tuple of characteristics used \
            to create groups ('country' will group airports by country)
        :param op: specify the type of operation \
            to calculate the value of aggregated data (sum, mean, ...)
        '''

        # aggregaData is a dictionary containing groups of airports sharing the
        # same characteristics (given by T_field)
        aggreg_data = {}
        exception_keys = []

        for key in dict_data:

            # First, we convert the key which is a tuple
            try:
                agg_key = self.multiGet(key , T_field)

            except KeyError as e:
                if not safe_mode:
                    raise
                else:
                    exception_keys.append(key)

            else:
                if agg_key not in aggreg_data:
                    aggreg_data[agg_key] = []

                aggreg_data[agg_key].append(dict_data[key])

        # For each group of airports, we can the result
        for agg_key, group in aggreg_data.iteritems():
            aggreg_data[agg_key] = _reduceGroup(group, op)

        if exception_keys and not quiet:
            print "/!\ Some keys were not found /!\: %s" % exception_keys

        return aggreg_data


    def getLocation(self, key):
        '''
        Returns proper geocode.

        >>> geo_a.getLocation('AGN')
        (57.50..., -134.585...)
        '''
        return float(self.get(key, 'lat')), float(self.get(key, 'lng'))


    def iterLocations(self):
        '''
        Returns all positions.

        :returns: a list of all (key, lat, lng) in the database

        >>> list(geo_a.iterLocations())
        [('AGN', (57.50..., -134.585...)), ('AGM', (65.6...
        '''
        return ( (key, self.getLocation(key)) for key in self._things )


    def iterField(self, field):
        '''
        Get list of all field data for all entries.
        For example, if you want to know all names of all things.

        :param field: the wanted field
        :returns:     a list of all fields, like ['Orly', 'Blagnac', ...] \
            for field 'name'

        >>> list(geo_t.iterField('name'))
        ['Ballersdorf', ...
        '''

        return ( self.get(key, field) for key in self._things )


    def __iter__(self):
        '''
        Returns iterator of all keys in the database.

        :returns: the list of all keys

        >>> list(a for a in geo_a)
        ['AGN', 'AGM', 'AGJ', 'AGH', ...
        '''
        return self._things.iterkeys()


    def __contains__(self, key):
        '''
        Test if a thing is in the base.

        :param key: the key of the thing to be tested
        :returns:   a boolean

        >>> 'AN' in geo_a
        False
        >>> 'AGN' in geo_a
        True
        '''
        if key in self._things:
            return True

        return False



    def keys(self):
        '''
        Returns a list of all keys in the database.

        :returns: the list of all keys

        >>> geo_a.keys()
        ['AGN', 'AGM', 'AGJ', 'AGH', ...
        '''

        return self._things.keys()



    def findNearPoint(self, lat, lng, radius=50):
        '''
        Returns a list of nearby things from a point (given
        latidude and longitude), and a radius for the search.
        Note that the haversine function, which compute distance
        at the surface of a sphere, here returns kilometers,
        so the radius should be in kms.

        :param lat:     the latitude of the point
        :param lng:     the longitude of the point
        :param radius:  the radius of the search (kilometers)
        :returns:       a list of keys of things (like ['ORY', 'CDG'])

        >>> # Paris, airports <= 50km
        >>> [geo_a.get(k, 'name') for d, k in geo_a.findNearPoint(48.84, 2.367, 50)]
        ['Paris-Orly', 'Paris-Le Bourget', 'Toussus-le-Noble', 'Paris - Charles-de-Gaulle']
        >>>
        >>> # Nice, stations <= 5km
        >>> [geo_t.get(k, 'name') for d, k in geo_t.findNearPoint(43.70, 7.26, 5)]
        ['Nice-Ville', 'Nice-Riquier', 'Nice-St-Roch', 'Villefranche-sur-Mer', 'Nice-St-Augustin']
        '''

        near = []

        for thing in self._things:

            dist = haversine(self.getLocation(thing), (lat, lng))

            if dist <= radius:
                near.append((dist, thing))

        return sorted(near)



    def findNearKey(self, key, radius=50):
        '''
        Same as findNearPoint, except the point is given
        not by a lat/lng, but with its key, like ORY or SFO.
        We just look up in the base to retrieve lat/lng, and
        call findNearPoint.

        :param key:     the key of the point
        :param radius:  the radius of the search (kilometers)
        :returns:       a list of keys of things (like ['ORY', 'CDG'])

        >>> geo_a.findNearKey('ORY', 50) # Orly, airports <= 50km
        [(0.0, 'ORY'), (18.8..., 'TNF'), (27.8..., 'LBG'), (34.8..., 'CDG')]
        >>> geo_t.findNearKey('frnic', 5) # Nice station, stations <= 5km
        [(0.0, 'frnic'), (2.2..., 'fr4342'), (2.3..., 'fr5737'), (4.1..., 'fr4708'), (4.5..., 'fr6017')]
        '''

        return self.findNearPoint(
            self.get(key, 'lat'),
            self.get(key, 'lng'),
            radius
        )


    def findClosestFromPoint(self, lat, lng, N=1, from_keys=None):
        '''
        Concept close to findNearPoint, but here we do not
        look for the things radius-close to a point,
        we look for the closest thing from this point, given by
        latitude/longitude.

        Note that a similar implementation is done in
        the LocalHelper, to find efficiently N closest point
        in a graph, from a point (using heaps).

        :param lat: the latitude of the point
        :param lng: the longitude of the point
        :param N:   the N closest results wanted
        :param from_keys: if None, it takes all keys in consideration, else takes from_keys \
            iterable of keys to perform findClosestFromPoint. This is useful when we have names \
            and have to perform a matching based on name and location (see fuzzyGetAroundLatLng).
        :returns:   one key (like 'SFO')

        >>> geo_a.findClosestFromPoint(43.70, 7.26) # Nice
        [(5.82..., 'NCE')]
        >>> geo_a.findClosestFromPoint(43.70, 7.26, N=3) # Nice
        [(5.82..., 'NCE'), (30.28..., 'CEQ'), (79.71..., 'ALL')]
        >>> geo_t.findClosestFromPoint(43.70, 7.26, N=1) # Nice
        [(0.56..., 'frnic')]
        >>> geo_t.findClosestFromPoint(43.70, 7.26, N=2, from_keys=('frpaz', 'frply', 'frbve')) # Nice
        [(482.84..., 'frbve'), (683.89..., 'frpaz')]
        '''

        if from_keys is None:
            from_keys = self._things.iterkeys()

        iterable = ( (haversine(self.getLocation(key), (lat, lng)), key) for key in from_keys )

        return heapq.nsmallest(N, iterable)



    def fuzzyGet(self, fuzzy_value, field='name', approximate=None, from_keys=None):
        '''
        We get to the cool stuff.

        Fuzzy searches are retrieving an information
        on a thing when we do not know the code.
        We compare the value fuzzy_value which is supposed to be a field
        (e.g. a city or a name), to all things we have in the database,
        and we output the best match.
        Matching is performed using Levenshtein module, with a modified
        version of the Lenvenshtein ratio, adapted to the type of data.

        Example: we look up 'Marseille Saint Ch.' in our database
        and we find the corresponding code by comparing all station
        names with ''Marseille Saint Ch.''.

        :param fuzzy_value: the value, like 'Marseille'
        :param field:       the field we look into, like 'name'
        :param approximate: if None, returns the best, if an int, returns a list of \
            n best matches
        :param from_keys: if None, it takes all keys in consideration, else takes from_keys \
            iterable of keys to perform fuzzyGet. This is useful when we have geocodes \
            and have to perform a matching based on name and location (see fuzzyGetAroundLatLng).
        :returns:           a couple with the best match and the distance found

        >>> geo_t.fuzzyGet('Marseille Charles', 'name')
        (0.8..., 'frmsc')
        >>> geo_a.fuzzyGet('paris de gaulle', 'name')
        (0.78..., 'CDG')
        >>> geo_a.fuzzyGet('paris de gaulle', 'name', approximate=3)
        [(0.78..., 'CDG'), (0.60..., 'HUX'), (0.57..., 'LBG')]
        '''

        if from_keys is None:
            # iter(self) would have worked since __iter__ is defined
            from_keys = self._things.iterkeys()

        # All 'intelligence' is performed in the Levenshtein
        # module just here. All we do is minimize this distance
        iterable = ( (mod_leven(fuzzy_value, self.get(key, field)), key)
                     for key in from_keys )

        if approximate is None:
            return max(iterable)

        return heapq.nlargest(approximate, iterable)



    def fuzzyGetAroundLatLng(self, lat, lng, radius, fuzzy_value, field='name', approximate=None):
        '''
        Same as fuzzyGet but with we search only within a radius
        from a geocode.

        :param lat:     the latitude of the point
        :param lng:     the longitude of the point
        :param radius:  the radius of the search (kilometers)
        :param fuzzy_value: the value, like 'Marseille'
        :param field:       the field we look into, like 'name'
        :param approximate: if None, returns the best, if an int, returns a list of \
            n best matches

        >>> geo_a.fuzzyGet('Brussels', 'name', approximate=None)
        (0.61..., 'BQT')
        >>> geo_a.get('BQT', 'name')  # Brussels just matched on Brest!!
        'Brest'
        >>> geo_a.multiGet(('BRU', 'BRU', 'BRU'), ('lat', 'lng', 'name')) # We wanted BRU for 'Bruxelles'
        ('50.90...', '4.48...', 'Bruxelles National')
        >>> # Now a request limited to a circle of 20km around BRU gives BRU
        >>> geo_a.fuzzyGetAroundLatLng('50.9013890', '4.4844440', 20, 'Brussels', 'name')
        (0.46..., 'BRU')
        '''

        nearest = ( key for dist, key in self.findNearPoint(lat, lng, radius) )

        return self.fuzzyGet(fuzzy_value, field, approximate, from_keys=nearest)


    def _fuzzyGetBiased(self, entry, verbose=True):
        '''
        Same as fuzzyGet but with bias system.
        '''

        if entry in self._bias_cache_fuzzy:
            # If the entry is stored is our bias
            # cache, we do not perform the fuzzy search
            # It avoids single failure on some rare examples
            if verbose:
                print 'Using bias: %s' % str(entry)

            return self._bias_cache_fuzzy[entry]

        # If not we process and store it in the cache
        return self.fuzzyGet(*entry)


    def fuzzyGetCached(self,
                        fuzzy_value,
                        field='name',
                        approximate=None,
                        verbose=True,
                        show_bad=(1, 1)):
        '''
        Same as fuzzyGet but with a caching and bias system.

        :param fuzzy_value: the value, like 'Marseille'
        :param field:       the field we look into, like 'name'
        :param approximate: if None, returns the best, if an int, returns a list of \
            n best matches
        :param verbose:     display a certain range of similarity
        :param show_bad:    the range of similarity
        :returns:           the best match

        >>> geo_t.fuzzyGetCached('Marseille Saint Ch.', 'name')
        (0.8..., 'frmsc')
        >>> geo_a.fuzzyGetCached('paris de gaulle', 'name', show_bad=(0, 1))
        [0.79]           paris+de+gaulle ->   paris+charles+de+gaulle (  CDG)
        (0.78..., 'CDG')
        >>> geo_a.fuzzyGetCached('paris de gaulle', 'name', approximate=2, show_bad=(0, 1))
        [0.79]           paris+de+gaulle ->   paris+charles+de+gaulle (  CDG)
        [0.61]           paris+de+gaulle ->        bahias+de+huatulco (  HUX)
        [(0.78..., 'CDG'), (0.60..., 'HUX')]

        Some biasing:

        >>> geo_a.biasFuzzyCache('paris de gaulle', 'name', None, 'Biased result')
        >>> geo_a.fuzzyGetCached('paris de gaulle', 'name', approximate=None, show_bad=(0, 1)) # Cache there
        (0.78..., 'CDG')
        >>> geo_a.clearCache()
        >>> geo_a.fuzzyGetCached('paris de gaulle', 'name', approximate=None)
        Using bias: ('paris+de+gaulle', 'name', None)
        'Biased result'
        '''

        # Cleaning is for keeping only useful data
        entry = self._buildCacheKey(fuzzy_value, field, approximate)

        if entry not in self._cache_fuzzy:

            match = self._fuzzyGetBiased(entry, verbose=verbose)

            self._cache_fuzzy[entry] = match

            # Debug purpose
            if verbose:
                self._debugFuzzy(match, fuzzy_value, field, approximate, show_bad)

        return self._cache_fuzzy[entry]



    def biasFuzzyCache(self, fuzzy_value, field, approximate, biased_result):
        '''
        If algorithms for fuzzy searches are failing on a single example,
        it is possible to use a first cache which will block
        the research and force the result.
        '''

        # Cleaning is for keeping only useful data
        entry = self._buildCacheKey(fuzzy_value, field, approximate)

        self._bias_cache_fuzzy[entry] = biased_result


    def clearCache(self):
        self._cache_fuzzy = {}

    def clearBiasCache(self):
        self._bias_cache_fuzzy = {}


    def _buildCacheKey(self, fuzzy_value, field, approximate):
        '''
        Key for the cache of fuzzyGet, based on parameters.

        >>> geo_a._buildCacheKey('paris de gaulle', 'name', approximate=None)
        ('paris+de+gaulle', 'name', None)
        >>> geo_a._buildCacheKey('Antibes SNCF 2', 'name', approximate=3)
        ('antibes', 'name', 3)
        '''
        return '+'.join(clean(fuzzy_value)), field, approximate


    def _debugFuzzy(self,
                    match,
                    fuzzy_value,
                    field,
                    approximate=None,
                    show_bad=(1, 1)):
        '''
        Some debugging.
        '''
        if approximate is None:
            matches = [ match ]
        else:
            matches = match

        for m in matches:

            if m[0] >= show_bad[0] and m[0] < show_bad[1]:

                print "[%.2f] %25s -> %25s (%5s)" % \
                    (m[0],
                     '+'.join(clean(fuzzy_value)),
                     '+'.join(clean(self.get(m[1], field))),
                     m[1])



    def haversine(self, key0, key1):
        '''
        Compute distance between two elements.
        This is just a wrapper between the original haversine
        function, but it is probably the most used feature :)

        :param key0: the first key
        :param key1: the second key
        :returns:    the distance (km)

        >>> geo_t.haversine('frnic', 'frpaz')
        683.526...
        '''

        return haversine(self.getLocation(key0), self.getLocation(key1))




    def set(self, key, field, value):
        '''
        Method to manually change a value in the base.

        :param key:   the key we want to change a value of
        :param field: the concerned field, like 'lat'
        :param value: the new value

        >>> geo_t.get('frnic', 'name')
        'Nice-Ville'
        >>> geo_t.set('frnic', 'name', 'Nice Gare SNCF')
        >>> geo_t.get('frnic', 'name')
        'Nice Gare SNCF'
        '''

        # If the key is not in the database,
        # we simply add it
        if key not in self._things:
            self._things[key] = {}

        self._things[key][field] = value

        # If the field was not referenced in the headers
        # we add it to the headers
        if field not in self._headers:
            self._headers.append(field)


    def setWithDict(self, key, dictionary):
        '''
        Same as set method, except we perform
        the input with a whole dictionary.

        :param key:         the key we want to change a value of
        :param dictionary:  the dict containing the new data

        >>> geo_m.keys()
        []
        >>> geo_m.setWithDict('frnic', {'code' : 'frnic', 'name': 'Nice'})
        >>> geo_m.keys()
        ['frnic']
        '''

        for field, val in dictionary.iteritems():
            self.set(key, field, val)


    def delete(self, key):
        '''
        Method to manually remove a value in the base.

        :param key:   the key we want to change a value of
        :param field: the concerned field, like 'lat'
        :returns:     None

        >>> data = geo_t.get('frxrn') # Output all data in one dict
        >>> geo_t.delete('frxrn')
        >>> geo_t.get('frxrn', 'name')
        Traceback (most recent call last):
        KeyError: 'Thing not found: frxrn'

        How to reverse the delete if data has been stored:

        >>> geo_t.setWithDict('frxrn', data)
        >>> geo_t.get('frxrn', 'name')
        'Redon'
        '''

        del self._things[key]


    def occurrences(self, field):
        '''
        Counts occurrences for a field.

        :param field: the concerned field, like 'city_code'
        :returns:     a list of occurrences

        >>> geo_a2 = GeoBase(data='airports_csv', verbose=False) # Other source
        >>> geo_a2.get('CDG', 'region_code')
        'EUROP'
        >>> geo_a2.occurrences('region_code')
        [(1, 'ID'),
         (5, 'ATLAN'),
         (19, 'NONE'),
         (24, 'IOCEA'),
         (146, 'EEURO'),
         (160, 'CAMER'),
         (181, 'CARIB'),
         (182, 'PACIF'),
         (289, 'MEAST'),
         (354, 'EURAS'),
         (471, 'SEASI'),
         (777, 'ASIA'),
         (1036, 'SAMER'),
         (1171, 'AFRIC'),
         (1290, 'AUSTL'),
         (1971, 'EUROP'),
         (3172, 'NAMER')]
        '''
        return sorted([
            (len(list(g)), k)
            for k, g in groupby(sorted(self.iterField(field)))
        ])


    def dumpJson(self, output_name, indent=4):
        '''
        Dumps a json representation of the structure.

        :param output_name: name of the output file
        :param indent:      if None, the json will not be indented
        :returns:           None
        '''

        json.dump(dict( (key, self.get(key)) for key in self ),
                  open(output_name, 'w'),
                  indent=indent)



def _reduceGroup(group, op):
    '''
    This method is protected and shouldnt be used.
    It computates a group of airports, after aggregation, to have
    the aggregate value for the group.

    >>> _reduceGroup([1, 2, 6], '+')
    9
    >>> _reduceGroup([1.0, 2, 6], '+')
    9.0
    >>> _reduceGroup([1, 2, 6], 'var')
    7.0
    >>> _reduceGroup([1, 2, 6], ('var', 'mean'))
    (7.0, 3.0)

    :param group: a dictionary of values
    :param op: a function indicating which operation the method will use int.__add__, int.__mul__ should work if values are int
    '''

    if not group:
        raise ValueError("Empty group")

    if isinstance(op, tuple):
        return tuple(_reduceGroup(group, o) for o in op)

    # Warning: will work for floats,
    # But for other classes sum(), /, ** have to be defined
    if op == 'mean': return np.mean(group)
    if op == 'var' : return np.var(group, ddof=1)
    if op == 'sd'  : return np.sd(group, ddof=1)

    if op == 'keep': return group

    if op == '+': op = group[0].__class__.__add__
    if op == '*': op = group[0].__class__.__mul__

    return reduce(op, group)


def _test():
    '''
    When called directly, launching doctests.
    '''

    import doctest
    extraglobs = {
        'geo_a': GeoBase(data='airports', verbose=False),
        'geo_t': GeoBase(data='stations', verbose=False),
        'geo_m': GeoBase(data='mix',      verbose=False)
    }

    opt =  (doctest.ELLIPSIS |
            doctest.NORMALIZE_WHITESPACE |
            doctest.REPORT_ONLY_FIRST_FAILURE |
            doctest.IGNORE_EXCEPTION_DETAIL)

    doctest.testmod(extraglobs=extraglobs, optionflags=opt)



if __name__ == '__main__':
    _test()



