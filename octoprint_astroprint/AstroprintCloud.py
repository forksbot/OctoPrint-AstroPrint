# coding=utf-8
__author__ = "AstroPrint Product Team <product@astroprint.com>"
__license__ = "GNU Affero General Public License http://www.gnu.org/licenses/agpl.html"
__copyright__ = "Copyright (C) 2017 3DaGoGo, Inc - Released under terms of the AGPLv3 License"

from flask import request, make_response, jsonify
import time
import json
from threading import Timer
import octoprint.filemanager
from .AstroprintDB import AstroprintDB, AstroprintUser
from .downloadmanager import DownloadManager
from .boxrouter import boxrouterManager
from requests_toolbelt import MultipartEncoder
import requests
import os
import octoprint.filemanager.util
from octoprint.filemanager.destinations import FileDestinations
from octoprint.server import VERSION
import sys
import platform

class AstroprintCloud():

	plugin = None
	appId = None
	token = None
	refresh_token = None
	expires = 10
	last_request = 0
	timer = None
	currentlyPrinting = None

	def __init__(self, plugin):
		self.plugin = plugin
		self.apiHost = plugin.get_settings().get(["apiHost"])
		self.appId = plugin.get_settings().get(["appId"])
		self.db = AstroprintDB(self.plugin)
		self.bm = boxrouterManager(self.plugin)
		self.downloadmanager = DownloadManager(self)
		self._logger = self.plugin.get_logger()
		self._printer = plugin.get_printer()
		self._file_manager = plugin.get_file_manager()
		user = self.db.getUser()
		self.plugin.cameraManager.astroprintCloud = self
		self.plugin.get_printer_listener().astroprintCloud = self
		self.statePayload = None

		if user:
			self.token = user.token
			self.refresh_token = user.refresh_token
			self.last_request = user.last_request
			self.expires = user.expires
			self.tokenManagment(user)
		else:
			self.connectBoxrouter()



	def tokenManagment(self, user):
		self.plugin.user = user
		if ( (self.expires - 300) >  round(time.time())):
			self.timer = Timer((self.expires - 300) -  round(time.time()), self.refresh)
			self.timer.daemon = True
			self.timer.start()
			self.connectBoxrouter()
		else:
			self.refresh(True)

	def refresh(self, login_refresh=False):
		try:
			r = requests.post(
				"%s/token" % (self.apiHost),
				data = {
					"client_id": self.appId,
					"grant_type": "refresh_token",
					"refresh_token": self.refresh_token
					},
			)
			r.raise_for_status()
			data = r.json()
			self.plugin.user.token = data['access_token']
			self.token = self.plugin.user.token
			self.plugin.user.refresh_token = data['refresh_token']
			self.refresh_token = self.plugin.user.refresh_token
			self.plugin.user.last_request = round(time.time())
			self.plugin.user.expires = round(self.plugin.user.last_request + data['expires_in'])
			self.expires = self.plugin.user.expires
			self.db.updateUser(self.plugin.user)
			self.timer = Timer(( data['expires_in'] - 300), self.refresh)
			self.timer.daemon = True
			self.timer.start()
			if (login_refresh):
				self.connectBoxrouter()
		except requests.exceptions.HTTPError as err:
				if err.response.status_code == 400 or err.response.status_code == 401:
					self._logger.warning("refresh token expired, AstroPrint user logged out.")
					self.plugin.send_event("logOut")
					self.unautorizedHandeler()
				else:
					self.timer = Timer(10, self.refresh)
					self.timer.daemon = True
					self.timer.start()
		except requests.exceptions.RequestException as e:
			print e

	def logAstroPrint(self, code, url):
		try:
			r = requests.post(
				"%s/token" % (self.apiHost),
				data = {
					"client_id": self.appId,
					"access_key" : self.plugin.access_key,
					"grant_type": "astroprint_access_key",
					"code": code,
					"redirect_uri": url
					},
			)
			r.raise_for_status()
			data = r.json()
			self.user = AstroprintUser()
			self.user.token = data['access_token']
			self.token = self.user.token
			self.user.refresh_token = data['refresh_token']
			self.refresh_token = self.user.refresh_token
			self.user.last_request = round(time.time())
			self.user.expires = round(self.user.last_request + data['expires_in'])
			self.expires = self.user.expires
			return self.getUserName()

		except requests.exceptions.HTTPError as err:
			self._logger.error(err.response.text)
			return jsonify(json.loads(err.response.text)), err.response.status_code, {'ContentType':'application/json'}
		except requests.exceptions.RequestException as e:
			self._logger.error(e)
			return jsonify({'error': "Internal server error"}), 500, {'ContentType':'application/json'}

	def getUserName(self):
		try:
			r = requests.get(
				"%s/accounts/me" % (self.apiHost),
				headers={'Content-Type': 'application/x-www-form-urlencoded',
						'authorization': ("bearer %s" %self.user.token) }
			)
			r.raise_for_status()
			data = r.json()
			self.user.userId = data['id']
			self.user.email = data['email']
			self.user.name = data['name']
			self.user.accessKey = self.plugin.access_key
			self.plugin.user = self.user
			self.db.saveUser(self.plugin.user)
			self.plugin.sendSocketInfo()
			self.timer = Timer((self.expires - 300) -  round(time.time()), self.refresh)
			self.timer.daemon = True
			self.timer.start()
			self.connectBoxrouter()
			self._logger.info("%s logged to AstroPrint" % self.user.name)
			user = {'name': self.user.name, 'email': self.user.email}
			return jsonify(user), 200, {'ContentType':'application/json'}

		except requests.exceptions.HTTPError as err:
			if (err.response.status_code == 401):
				self.unautorizedHandeler()
			return jsonify(json.loads(err.response.text)), err.response.status_code, {'ContentType':'application/json'}
		except requests.exceptions.RequestException as e:
			return jsonify({'error': "Internal server error"}), 500, {'ContentType':'application/json'}

	def logoutAstroPrint(self):
		self.unautorizedHandeler()
		return jsonify({"Success" : True }), 200, {'ContentType':'application/json'}

	def unautorizedHandeler (self):
		self.db.deleteUser(self.plugin.user)
		self.plugin.user = None
		self.disconnectBoxrouter()
		if self.timer:
			self.timer.cancel()
			self.timer = None
		self.plugin.astroPrintUserLoggedOut()

	def printStarted(self, name, path):

		print_file = self.db.getPrintFileByOctoPrintPath(path)
		print_file_id = print_file.printFileId if print_file else None
		print_file_name = print_file.printFileName if print_file else name

		self.startPrintJob(print_file_id, print_file_name)

	def startPrintJob(self, print_file_id= None, print_file_name= None):
		try:
			data = {
					"box_id": self.bm.boxId,
					"product_variant_id": self.plugin.get_settings().get(["product_variant_id"]),
					"name": print_file_name,
					}

			if print_file_id:
				data['print_file_id'] = print_file_id


			r = requests.post(
				"%s/print-jobs" % (self.apiHost),
				json = data,
				headers={'Content-Type': 'application/x-www-form-urlencoded',
						'authorization': ("bearer %s" %self.plugin.user.token)},
				stream=True
			)

			data = r.json()
			self.currentlyPrinting = data['id']

		except requests.exceptions.HTTPError as err:
			if (err.response.status_code == 401):
				self.unautorizedHandeler()
			self._logger.error("Failed to send print_job request: %s" % err.response.text)
		except requests.exceptions.RequestException as e:
			self._logger.error("Failed to send print_job request: %s" % e)

	def updatePrintJob(self, status, totalConsumedFilament):
		try:

			data = {'status': status}

			if totalConsumedFilament:
				data['material_used'] = totalConsumedFilament

			r = requests.post(
				"%s/print-jobs/%s" % (self.apiHost, self.currentlyPrinting),
				json = data,
				headers={'Content-Type': 'application/x-www-form-urlencoded',
						'authorization': ("bearer %s" %self.plugin.user.token)},
				stream=True
			)

		except requests.exceptions.HTTPError as err:
			if (err.response.status_code == 401):
				self.unautorizedHandeler()
			self._logger.error("Failed to send print_job request: %s" % err.response.text)
		except requests.exceptions.RequestException as e:
			self._logger.error("Failed to send print_job request: %s" % e)

	def connectBoxrouter(self):

		if self.plugin.user and self.plugin.user.accessKey and self.plugin.user.userId:
			self.bm.boxrouter_connect()
			#let the singleton be recreated again, so new credentials are taken into use
			global _instance
			_instance = None
			return True

		return False

	def disconnectBoxrouter(self):
		self.bm.boxrouter_disconnect()

	def sendCurrentData(self):

		payload = {
			'operational': self.plugin.get_printer().is_operational(),
			'printing': self.plugin.get_printer().is_paused() or self.plugin._printer.is_printing(),
			'paused': self.plugin.get_printer().is_paused(),
			'camera': self.plugin.cameraManager.cameraActive,
			'heatingUp': self.plugin.printerIsHeating()
		}

	 	if self.statePayload != payload and self.bm:
			self.bm.broadcastEvent('status_update', payload)
			self.statePayload = payload


	def printFile(self, printFileId):
		printFile = self.db.getPrintFileById(printFileId)
		if printFile:
			self.printFileIsDownloaded(printFile)
			return "print"
		else:
			printFile = self.addPrintfileDownloadUrl(self.getPrintFileInfoForDownload(printFileId))
			if printFile:
				if not self.downloadmanager.isDownloading(printFileId):
					self.downloadmanager.startDownload(printFile)
				return "download"
			return None

	def getPrintFileInfoForDownload(self, printFileId):
		self._currentDownlading = printFileId
		self._downloading= True
		try:
			r = requests.get(
				"%s/printfiles/%s" % (self.apiHost, printFileId),
				headers={'Content-Type': 'application/x-www-form-urlencoded',
						'authorization': ("bearer %s" %self.plugin.user.token) },
						stream=True
			)
			printFile = r.json()
			r.raise_for_status()
			if printFile['format'] == 'gcode' :
				return printFile
			else:
				payload = {
					"id" : printFileId,
					"type" : "error",
					"reason" : "Invalid file extension"
				}
				self.bm.triggerEvent('onDownload', payload)

		except requests.exceptions.HTTPError as err:
			payload = {
				"id" : printFile['id'],
				"type" : "error",
				"reason" : err.response.text
			}
			self.bm.triggerEvent('onDownload', payload)
			if (err.response.status_code == 401):
				self.unautorizedHandeler()
			return None
		except requests.exceptions.RequestException as e:
			payload = {
				"id" : printFile['id'],
				"type" : "error",
			}
			self.bm.triggerEvent('onDownload', payload)
			return None

	def addPrintfileDownloadUrl(self, printFile):
		if not printFile:
			return None
		try:
			r = requests.get(
				"%s/printfiles/%s/download?download_info=true" % (self.apiHost, printFile['id']),
				headers={'Content-Type': 'application/x-www-form-urlencoded',
						'authorization': ("bearer %s" %self.plugin.user.token) },
						stream=True
			)
			downloadInfo = r.json()
			printFile['download_url'] = downloadInfo['download_url']
			r.raise_for_status()

			return printFile

		except requests.exceptions.HTTPError as err:

			payload = {
				"id" : printFile['id'],
				"type" : "error",
				"reason" : err.response.text
			}
			self.bm.triggerEvent('onDownload', payload)
			if (err.response.status_code == 401):
				self.unautorizedHandeler()
			return None
		except requests.exceptions.RequestException as e:

			payload = {
				"id" : printFile['id'],
				"type" : "error",
			}

			self.bm.triggerEvent('onDownload', payload)
			return None


	def wrapAndSave(self, fileType, file, printNow=False):

		name = file if fileType == "design" else file.printFileName
		filepath = ("%s/%s" %(self.plugin._basefolder, name))
		fileObject = octoprint.filemanager.util.DiskFileWrapper(name, filepath)

		try:
			self._file_manager.add_file(FileDestinations.LOCAL, name, fileObject, allow_overwrite=True)
			if fileType == "printFile":
				self.db.savePrintFile(file)
				if printNow:
					self.printFileIsDownloaded(file)
			return None

		except octoprint.filemanager.storage.StorageError as e:
			if os.path.exists(filepath):
				os.remove(filepath)
				if e.code == octoprint.filemanager.storage.StorageError.INVALID_FILE:
					payload = {
						"id" : printFile.printFileId,
						"type" : "error",
						"reason" : e.code
					}
					self.bm.triggerEvent('onDownload', payload)
					return None
				elif e.code == octoprint.filemanager.storage.StorageError.ALREADY_EXISTS:
					payload = {
						"id" : printFile.printFileId,
						"type" : "error",
						"reason" : e.code
					}
					self.bm.triggerEvent('onDownload', payload)
					return None
				else:
					payload = {
						"id" : printFile.printFileId,
						"type" : "error",
					}
					self.bm.triggerEvent('onDownload', payload)
					return None
			else:
				return None

	def printFileIsDownloaded(self, printFile):
		if self._printer.is_printing():
			isBeingPrinted = False
		else:
			self._printer.select_file(self._file_manager.path_on_disk(FileDestinations.LOCAL, printFile.printFileName), False, True)
			if self._printer.is_printing():
				isBeingPrinted = True
			else:
				isBeingPrinted = False
		self.bm.triggerEvent('onDownloadComplete', {"id": printFile.printFileId, "isBeingPrinted": isBeingPrinted})

	def getDesigns(self):
		try:
			r = requests.get(
				"%s/designs" % (self.apiHost),
				headers={'Content-Type': 'application/x-www-form-urlencoded',
						'authorization': ("bearer %s" %self.plugin.user.token) }
			)
			r.raise_for_status()
			data = r.json()
			return jsonify(data), 200, {'ContentType':'application/json'}

		except requests.exceptions.HTTPError as err:
			if (err.response.status_code == 401):
				self.unautorizedHandeler()
			return jsonify(json.loads(err.response.text)), err.response.status_code, {'ContentType':'application/json'}
		except requests.exceptions.RequestException as e:
			return jsonify({'error': "Internal server error"}), 500, {'ContentType':'application/json'}

	def getDesignDownloadUrl(self, designId, name):
		try:
			r = requests.get(
				"%s/designs/%s/download" % (self.apiHost, designId),
				headers={'Content-Type': 'application/x-www-form-urlencoded',
						'authorization': ("bearer %s" %self.plugin.user.token) }
			)
			r.raise_for_status()
			data = r.json()

			self.downloadmanager.startDownload({"id": designId, "name" : name, "download_url" : data['download_url'], "designDownload" : True})
			return jsonify({"Success" : True }), 200, {'ContentType':'application/json'}
		except requests.exceptions.HTTPError as err:
			if (err.response.status_code == 401):
				self.unautorizedHandeler()
			return jsonify(json.loads(err.response.text)), err.response.status_code, {'ContentType':'application/json'}
		except requests.exceptions.RequestException as e:
			return jsonify({'error': "Internal server error"}), 500, {'ContentType':'application/json'}

	def getPrintFiles(self, designId):

		try:
			r = requests.get(
				"%s/designs/%s/printfiles" % (self.apiHost, designId),
				headers={'Content-Type': 'application/x-www-form-urlencoded',
						'authorization': ("bearer %s" %self.plugin.user.token) }
			)
			r.raise_for_status()
			data = r.json()
			return jsonify(data), 200, {'ContentType':'application/json'}

		except requests.exceptions.HTTPError as err:
			if (err.response.status_code == 401):
				self.unautorizedHandeler()
			return jsonify(json.loads(err.response.text)), err.response.status_code, {'ContentType':'application/json'}
		except requests.exceptions.RequestException as e:
			return jsonify({'error': "Internal server error"}), 500, {'ContentType':'application/json'}

	def cancelDownload(self, printFileId):
		self.downloadmanager.cancelDownload(printFileId)


	def startPrintCapture(self, filename, filepath):
		data = {'name': filename}

		astroprint_print_file = self.db.getPrintFileByOctoPrintPath(filepath)

		if astroprint_print_file:
			data['print_file_id'] = astroprint_print_file.printFileId

		if self.currentlyPrinting:
			data['print_job_id'] = self.currentlyPrinting

		try:

			r = requests.post(
				"%s/timelapse" % self.apiHost,
				headers={'Content-Type': 'application/x-www-form-urlencoded',
						'authorization': ("bearer %s" %self.plugin.user.token) },
				stream=True, timeout= (10.0, 60.0),
				data = data
			)

			status_code = r.status_code

		except:
			status_code = 500

		if status_code == 201:
			data = r.json()
			return {
				"error": False,
				"print_id": data['print_id']
			}

		if status_code == 402:
			return {
				"error": "no_storage"
			}

		else:
			return {
				"error": "unable_to_create"
			}

	def uploadImageFile(self, print_id, imageBuf):
			try:
				m = MultipartEncoder(fields=[('file',('snapshot.jpg', imageBuf))])
				r = requests.post(
					"%s/timelapse/%s/image" % (self.apiHost, print_id),
					data= m,
					headers= {'Content-Type': m.content_type,
					'authorization': ("bearer %s" %self.plugin.user.token) }
				)
				m = None #Free the memory?
				status_code = r.status_code

			except requests.exceptions.HTTPError as err:
				status_code = 500
			except requests.exceptions.RequestException as e:
				status_code = 500

			if status_code == 201:
				data = r.json()
				return data
			else:
				return None





