# coding=utf-8
__author__ = "AstroPrint Product Team <product@astroprint.com>"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'
__copyright__ = "Copyright (C) 2017 3DaGoGo, Inc - Released under terms of the AGPLv3 License"

import time
from octoprint.printer import PrinterCallback
from octoprint_astroprint.gCodeAnalyzer import GCodeAnalyzer

class PrinterListener(PrinterCallback):

	cameraManager = None	#set up when initialiced
	astroprintCloud = None
	_analyzed_job_layers = None

	def __init__(self, plugin):
		self._router = None
		self._plugin = plugin
		self._printer = self._plugin.get_printer()
		self._logger = self._plugin.get_logger()
		self._progress = None
		self._state = None
		self._job_data = None
		self._currentLayer = None
		self._timePercentPreviuosLayers = None
		self.last_layer_time_percent = None
		self._last_time_send = None

	def addWatcher(self, socket):
		self._router = socket

	def removeWatcher(self):
		self._router = None

	def get_current_layer(self):
		return self._currentLayer

	def get_analyzed_job_layers(self):
		return self._analyzed_job_layers

	def startPrint(self, file):
			self._analyzed_job_layers = None
			self._currentLayer = 0
			self.last_layer_time_percent = 0
			self._timePercentPreviuosLayers = 0
			self._printStartedAt = None
			self.timerCalculator = GCodeAnalyzer(file,True,self.cbGCodeAnalyzerReady,self.cbGCodeAnalyzerFail,self, self._plugin)
			self.timerCalculator.makeCalcs()

	def cbGCodeAnalyzerReady(self,timePerLayers,totalPrintTime,layerCount,size,layer_height,total_filament,parent):
		self._analyzed_job_layers = {}
		self._analyzed_job_layers["timePerLayers"] = timePerLayers
		self._analyzed_job_layers["layerCount"] = layerCount
		self._analyzed_job_layers["totalPrintTime"] = totalPrintTime*1.07

	def cbGCodeAnalyzerFail(self, parameters):
		self._logger.error("Fail to analyze Gcode: %s" % parameters['filename'])

	def updateAnalyzedJobInformation(self, progress):
		analyzedInformation = {"current_layer" : 0, "time_percent_previuos_layers" : 0}
		layerChanged = False
		if not self._currentLayer:
			self._currentLayer = 1

		if self._analyzed_job_layers:
			while self._analyzed_job_layers["timePerLayers"][self._currentLayer -1]['upperPercent'] < progress:
				layerChanged = True
				self._currentLayer+=1

			if layerChanged:
				if not self._currentLayer == 1:
					self._timePercentPreviuosLayers += self._analyzed_job_layers["timePerLayers"][self._currentLayer -2 ]['time']
				else:
					self._timePercentPreviuosLayers = 0

				self.cameraManager.layerChanged()
				self._plugin.sendSocketInfo()

	def on_printer_add_temperature(self, data):
		if self._router:
			payload = {}

			if 'bed' in data:
				payload['bed'] = { 'actual': data['bed']['actual'], 'target': data['bed']['target'] }

			if 'tool0' in data:
				payload['tool0'] = { 'actual': data['tool0']['actual'], 'target': data['tool0']['target'] }
			self._router.broadcastEvent('temp_update', payload)

	def on_printer_send_current_data(self, data):
		self.set_state(data)
		self.set_job_data(data['job'])
		self.set_progress(data)

	def set_state(self, data):
		flags = data['state']['flags']
		payload = {
			'operational': flags['operational'],
			'printing': flags['printing'] or flags['paused'],
			'paused': flags['paused'],
			'camera': self.cameraManager.cameraActive,
			'heatingUp': self._plugin.printerIsHeating(),
			'state': data['state']['text'].lower()
		}
		if self._plugin.printerIsHeating():
			self._last_time_send = 0
		if payload != self._state:
			self._plugin.sendSocketInfo()
			if self._router:
				self._router.broadcastEvent('status_update', payload)
		self._state = payload


	def set_job_data(self, data):
		if data['file']['name'] and data['file']['size']:
			cloudPrintFile = None
			if data['file']['origin'] == 'local':
				cloudPrintFile = self._plugin.astroprintCloud.db.getPrintFileByOctoPrintPath(data['file']['path'])
			payload = {
				"estimatedPrintTime": data['estimatedPrintTime'],
				"layerCount": self._analyzed_job_layers['layerCount'] if self._analyzed_job_layers else None,
				"file": {
					"origin": data['file']['origin'],
					"rendered_image": cloudPrintFile.renderedImage if cloudPrintFile else None,
					"name": data['file']['name'],
					"cloudId": cloudPrintFile.printFileId if cloudPrintFile else None,
					"date": data['file']['date'],
					"printFileName":data['file']['name'],
					"size": data['file']['size']
					},
				"filament": data['filament']
			}
		else:
			payload = None

		if payload != self._job_data:
			self._plugin.sendSocketInfo()
			self._job_data = payload

	def get_job_data(self):
		if not self._job_data:
			return None
		if not self._job_data['layerCount']:
			 self._job_data['layerCount'] = self._analyzed_job_layers['layerCount'] if self._analyzed_job_layers else None
		return self._job_data

	def set_progress(self, data):
		if data['progress']['printTime']:
			payload = self.time_adjuster(data['progress'])
		else :
			self._last_time_send = 0
			payload= None
		if payload != self._progress and self._router:
			self._router.broadcastEvent('printing_progress', payload)
		self._progress = payload

	def get_progress(self):
		return self._progress


	def time_adjuster(self, data):
		if not self._printStartedAt:
			self._printStartedAt = data['printTime']
		if not self._analyzed_job_layers:
			data['currentLayer'] = 0
			return data
		else:
			self.updateAnalyzedJobInformation(data['completion']/100)
			#timePercentPreviuosLayers = self._timePercentPreviuosLayers - self.last_layer_time_percent
			timePercentPreviuosLayers = self._timePercentPreviuosLayers
			data['currentLayer'] = self._currentLayer

			try:
				layerFileUpperPercent = self._analyzed_job_layers["timePerLayers"][self._currentLayer-1]['upperPercent']

				if self._currentLayer > 1:
					layerFileLowerPercent = self._analyzed_job_layers["timePerLayers"][self._currentLayer-2]['upperPercent']
				else:
					layerFileLowerPercent = 0

				currentAbsoluteFilePercent = data['completion']/100
				elapsedTime = data['printTime']

				try:
					currentLayerPercent = (currentAbsoluteFilePercent - layerFileLowerPercent) / (layerFileUpperPercent - layerFileLowerPercent)
				except:
					currentLayerPercent = 0

				layerTimePercent = currentLayerPercent * self._analyzed_job_layers["timePerLayers"][self._currentLayer-1]['time']

				currentTimePercent = self._timePercentPreviuosLayers + layerTimePercent

				estimatedTimeLeft = self._analyzed_job_layers["totalPrintTime"] * ( 1.0 - currentTimePercent )

				elapsedTimeVariance = elapsedTime - ( self._analyzed_job_layers["totalPrintTime"] - estimatedTimeLeft)
				adjustedEstimatedTime = self._analyzed_job_layers["totalPrintTime"] + elapsedTimeVariance
				estimatedTimeLeft = ( adjustedEstimatedTime * ( 1.0 - currentTimePercent ) )

				if  data['printTimeLeft'] and  data['printTimeLeft'] < estimatedTimeLeft:
					estimatedTimeLeft =  data['printTimeLeft']
				#we prefer to freeze time rather than increase it
				if self._last_time_send > estimatedTimeLeft or self._last_time_send is 0:
					self._last_time_send = estimatedTimeLeft
				else:
					estimatedTimeLeft = self._last_time_send

				data['currentLayer'] = self._currentLayer
				data['printTimeLeft'] = estimatedTimeLeft

				return data
			except Exception, e:
				return data

	def getTimePercenCurrentLayer(currentLayer):
		try:
			timePercentPreviousLayers = 0
			if not currentLayer == 1:
				currentLayer -= 2
				for layer in self._analyzed_job_layers["timePerLayers"]:
					timePercentPreviousLayers += self._comm.timePerLayers[layer-2]['time']
				return timePercentPreviousLayers
			else:
				return 0
		except:
			return 0

