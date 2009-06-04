__doc__= """RangeThreshold
Make threshold comparisons dynamic by using TALES expresssions,
rather than just number bounds checking.
"""

import rrdtool
from AccessControl import Permissions

from Globals import InitializeClass
from Products.ZenModel.ThresholdClass import ThresholdClass
from Products.ZenModel.ThresholdInstance import ThresholdInstance, ThresholdContext
from Products.ZenEvents import Event
from Products.ZenEvents.ZenEventClasses import Perf_Snmp
from Products.ZenUtils.ZenTales import talesEval, talesEvalStr
from Products.ZenEvents.Exceptions import pythonThresholdException, \
        rpnThresholdException

import logging
log = logging.getLogger('zen.RangeThreshold')

from Products.ZenUtils.Utils import unused
import types


def rpneval(value, rpn):
    """
    Simulate RPN evaluation: only handles simple arithmetic
    """
    if value is None: return value
    operators = ('+','-','*','/')
    rpn = rpn.split(',')
    rpn.reverse()
    stack = [value]
    while rpn:
        next = rpn.pop()
        if next in operators:
            first = stack.pop()
            second = stack.pop()
            try:
                value = eval('%s %s %s' % (second, next, first))
            except ZeroDivisionError:
                value = 0
            stack.append(value)
        elif next.upper() == 'ABS':
            stack.append(abs(float(stack.pop())))
        else:
            stack.append(float(next))
    return stack[0]


class RangeThreshold(ThresholdClass):
    """
    Threshold class that can evaluate RPNs and Python expressions
    """

    minval = ""
    maxval = ""
    eventClass = Perf_Snmp
    severity = 3
    escalateCount = 0

    _properties = ThresholdClass._properties + (
        {'id':'minval',        'type':'string',  'mode':'w'},
        {'id':'maxval',        'type':'string',  'mode':'w'},
        {'id':'eventClass',    'type':'string',  'mode':'w'},
        {'id':'severity',      'type':'int',     'mode':'w'},
        {'id':'escalateCount', 'type':'int',     'mode':'w'},
        )

    factory_type_information = (
        {
        'immediate_view' : 'editRRDRangeThreshold',
        'actions'        :
        (
        { 'id'            : 'edit'
          , 'name'          : 'Range Threshold'
          , 'action'        : 'editRRDRangeThreshold'
          , 'permissions'   : ( Permissions.view, )
          },
        )
        },
        )

    def createThresholdInstance(self, context):
        """Return the config used by the collector to process point
        thresholds. (id, pointval, severity, escalateCount)
        """
        mmt = RangeThresholdInstance(self.id,
                                      ThresholdContext(context),
                                      self.dsnames,
                                      minval=self.getMinval(context),
                                      maxval=self.getMaxval(context),
                                      eventClass=self.eventClass,
                                      severity=self.severity,
                                      escalateCount=self.escalateCount)
        return mmt
    def getMinval(self, context):
        """Build the min value for this threshold.
        """
        minval = None
        if self.minval:
            try:
                minval = talesEval("python:"+self.minval, context)
            except:
                msg= "User-supplied Python expression (%s) for minimum value caused error: %s" % \
                           ( self.minval,  self.dsnames )
                log.error( msg )
                raise pythonThresholdException(msg)
                minval = None
        return minval


    def getMaxval(self, context):
        """Build the max value for this threshold.
        """
        maxval = None
        if self.maxval:
            try:
                maxval = talesEval("python:"+self.maxval, context)
            except:
                msg= "User-supplied Python expression (%s) for maximum value caused error: %s" % \
                           ( self.maxval,  self.dsnames )
                log.error( msg )
                raise pythonThresholdException(msg)
                maxval = None
        return maxval

InitializeClass(RangeThreshold)
RangeThresholdClass = RangeThreshold



class RangeThresholdInstance(ThresholdInstance):
    # Not strictly necessary, but helps when restoring instances from
    # pickle files that were not constructed with a count member.
    count = {}

    def __init__(self, id, context, dpNames,
                 minval, maxval, eventClass, severity, escalateCount):
        self.count = {}
        self._context = context
        self.id = id
        self.minimum = minval
        self.maximum = maxval
        self.eventClass = eventClass
        self.severity = severity
        self.escalateCount = escalateCount
        self.dataPointNames = dpNames
        self._rrdInfoCache = {}

    def name(self):
        "return the name of this threshold (from the ThresholdClass)"
        return self.id

    def context(self):
        "Return an identifying context (device, or device and component)"
        return self._context

    def dataPoints(self):
        "Returns the names of the datapoints used to compute the threshold"
        return self.dataPointNames

    def rrdInfoCache(self, dp):
        if dp in self._rrdInfoCache:
            return self._rrdInfoCache[dp]
        data = rrdtool.info(self.context().path(dp))
        # handle both old and new style RRD versions   
        try:
            # old style 1.2.x
            value = data['step'], data['ds']['ds0']['type']
        except KeyError: 
            # new style 1.3.x
            value = data['step'], data['ds[ds0].type']
        self._rrdInfoCache[dp] = value
        return value

    def countKey(self, dp):
        return(':'.join(self.context().key()) + ':' + dp)

    def getCount(self, dp):
        countKey = self.countKey(dp)
        if not self.count.has_key(countKey):
            return None
        return self.count[countKey]

    def incrementCount(self, dp):
        countKey = self.countKey(dp)
        if not self.count.has_key(countKey):
            self.resetCount(dp)
        self.count[countKey] += 1
        return self.count[countKey]

    def resetCount(self, dp):
        self.count[self.countKey(dp)] = 0

    def fetchLastValue(self, dp, cycleTime):
        """
        Fetch the most recent value for a data point from the RRD file.
        """
        startStop, names, values = rrdtool.fetch(self.context().path(dp),
            'AVERAGE', '-s', 'now-%d' % (cycleTime*2), '-e', 'now')
        values = [ v[0] for v in values if v[0] is not None ]
        if values: return values[-1]

    def check(self, dataPoints):
        """The given datapoints have been updated, so re-evaluate.
        returns events or an empty sequence"""
        unused(dataPoints)
        result = []
        for dp in self.dataPointNames:
            cycleTime, rrdType = self.rrdInfoCache(dp)
            result.extend(self.checkRange(
                dp, self.fetchLastValue(dp, cycleTime)))
        return result

    def checkRaw(self, dataPoint, timeOf, value):
        """A new datapoint has been collected, use the given _raw_
        value to re-evalue the threshold."""
        unused(timeOf)
        result = []
        if value is None: return result
        try:
            cycleTime, rrdType = self.rrdInfoCache(dataPoint)
        except Exception:
            log.exception('Unable to read RRD file for %s' % dataPoint)
            return result
        if rrdType != 'GAUGE' and value is None:
            value = self.fetchLastValue(dataPoint, cycleTime)
        result.extend(self.checkRange(dataPoint, value))
        return result

    def checkRange(self, dp, value):
        'Check the value for range thresholds'
        log.debug("Checking %s %s falls between min %s and max %s",
                  dp, value, self.minimum, self.maximum)
        if value is None:
            return []
        if type(value) in types.StringTypes:
            value = float(value)
        thresh = None
        if self.minimum is not None and self.maximum is not None \
                and value >= self.minimum  and value <= self.maximum:
            thresh = value
            how = 'is between defined range values'
        if thresh is not None:
            severity = self.severity
            count = self.incrementCount(dp)
            if self.escalateCount and count >= self.escalateCount:
                severity = min(severity + 1, 5)
            summary = 'Threshold of %s %s: current value %.2f' % (
                self.name(), how, float(value))
            return [dict(device=self.context().deviceName,
                         summary=summary,
                         eventKey=self.id,
                         eventClass=self.eventClass,
                         component=self.context().componentName,
                         severity=severity)]
        else:
            count = self.getCount(dp)
            if count is None or count > 0:
                summary = 'Threshold of %s restored: current value: %.2f' % (
                    self.name(), value)
                self.resetCount(dp)
                return [dict(device=self.context().deviceName,
                             summary=summary,
                             eventKey=self.id,
                             eventClass=self.eventClass,
                             component=self.context().componentName,
                             severity=Event.Clear)]
        return []

    def raiseRPNExc( self ):
        """
        Raise an RPN exception, taking care to log all details.
        """
        msg= "The following RPN exception is from user-supplied code."
        log.exception( msg )
        raise rpnThresholdException(msg)


    def getGraphElements(self, template, context, gopts, namespace, color,
                         legend, relatedGps):
        """Produce a visual indication on the graph of where the
        threshold applies."""
        unused(template, namespace)
        if not color.startswith('#'):
            color = '#%s' % color
        minval = self.minimum
        maxval = self.maximum
        if not self.dataPointNames:
            return gopts
        gp = relatedGps[self.dataPointNames[0]]

        # Attempt any RPN expressions
        rpn = getattr(gp, 'rpn', None)
        if rpn:
            try:
                rpn = talesEvalStr(rpn, context)
            except:
                self.raiseRPNExc()
                return gopts

            try:
                minval = rpneval(minval, rpn)
            except:
                minval= 0
                self.raiseRPNExc()

            try:
                maxval = rpneval(maxval, rpn)
            except:
                maxval= 0
                self.raiseRPNExc()

        result = []
        if minval:
            result += [
                "HRULE:%s%s:%s\\j" % (minval, color,
                          legend or self.getMinLabel(minval, relatedGps)),
                ]
        if maxval:
            result += [
                "HRULE:%s%s:%s\\j" % (maxval, color,
                          legend or self.getMaxLabel(maxval, relatedGps))
                ]
        return gopts + result

    def getMinLabel(self, minval, relatedGps):
        """build a label for a Range threshold"""
        return "Range %s >= %s" % (self.getNames(relatedGps), self.setPower(minval))


    def getMaxLabel(self, maxval, relatedGps):
        """build a label for a Range threshold"""
        return "Range %s <= %s" % (self.getNames(relatedGps), self.setPower(maxval))




    def getNames(self, relatedGps):
        legends = [ getattr(gp, 'legend', gp) for gp in relatedGps.values() ] 
        return ', '.join(legends) 

    def setPower(self, number):
        powers = ("k", "M", "G")
        if number < 1000: return number
        for power in powers:
            number = number / 1000.0
            if number < 1000:  
                return "%0.2f%s" % (number, power)
        return "%.2f%s" % (number, powers[-1])

from twisted.spread import pb
pb.setUnjellyableForClass(RangeThresholdInstance, RangeThresholdInstance)
