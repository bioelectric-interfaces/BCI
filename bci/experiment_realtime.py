# -*- coding: utf-8 -*-
"""
Created on Thu Jun 28 18:13:30 2018

@author: Александр/AlexVosk
"""

import time
import socket
import struct
import numpy as np

from emg.EMGdecode import EMGDecoder
from emg.EMGfilter import envelopeFilter


class ExperimentRealtime():
    def __init__(self, config, pnhandler, inlet_amp, stimulator = None):
        self.config = config
        self.kalman_time = 0
        
        # init pnhandler, inlet_amp, stimulator and decoder/filter
        self.pnhandler = pnhandler
        self.inlet_amp = inlet_amp
        self.stimulator = stimulator
        self.decoder = None
        self.filter = None
    
        
        # params of amp data stream
        self.numCh = self.config['amp_config'].getint('n_channels_amp')
        self.offCh = self.numCh
        self.srate = self.config['amp_config'].getint('fs_amp')

        # time of realtime experiment (inf if it set to -1)
        experiment_time = self.config['realtime'].getint('experiment_time_realtime')
        self.experiment_time = float('inf') if experiment_time < 0 else experiment_time * self.srate



        avatar_freq = self.config['avatar'].getint('avatar_freq')
        self.avatar_period = 1 / avatar_freq
        self.avatar_buffer_size = self.config['avatar'].getint('avatar_buffer_size')
        
        # buffer for plotting
        self.coordbuff = []
        
        # PN channels for finger joints
        self.pn_fingers_range = np.asarray(list(map(int, self.config['avatar']['pn_fingers_coords'].split())))
        # avatar channels for finger joints
        self.avatar_fingers_range = np.asarray(list(map(int, self.config['avatar']['avatar_fingers_coords'].split())))
        
        # buffer
        self.avatar_buffer = np.zeros(self.avatar_buffer_size)
        
        # initializing Avatar connection
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.server_address = ('127.0.0.1', 12001)
        self.client_address = ('127.0.0.1', 9001)
        self.sock.bind(self.server_address)
        self._printm('UDP from {} {} established'.format(*self.server_address))
        
        self.stimulator.configurate(self.config['stimulation'].getint('electrode_start'),
                                    self.config['stimulation'].getint('electrode_stop'))
        
        self.stimulation_time = self.config['stimulation'].getint('stimulation_time')
        self.refractory_time = self.config['stimulation'].getint('refractory_time')
        self.refractory_start = 0
        self.bias = 45
        
        

        
    # fit data from file
    def fit(self):
        self._printm('fitting...')
        # fit the model from file and initialize filter
        self.decoder = EMGDecoder()
        self.decoder.fit(X = None, Y = None, 
                       file=self.config['paths']['experiment_data_to_fit_path'], 
                       numCh = self.numCh, offCh = self.offCh, 
                       Pn = self.pn_fingers_range, 
                       lag = 2, forward = 0, downsample = 0)
        self.filter = envelopeFilter()
        
    
    # decode realtime data
    def decode(self):
        self._printm('decoding...')
        
        # initialize pn_buffer and KalmanCoords
        pn_buffer = np.zeros(len(self.pn_fingers_range))
        KalmanCoords = np.zeros((1, self.numCh))
        
        # get chunks, decode and sent to avatar
        start_time = time.time()
        counter = 0
        counter_messages = 0
        while (time.time() - start_time < self.experiment_time):
            cycle_start_time = time.time()
        
            # get chunks of data from inlets
            chunk_pn, _ = self.pnhandler.get_next_chunk_pn()
            ampchunk, _ = self.inlet_amp.pull_chunk(max_samples=20)
            chunk_amp = np.asarray(ampchunk)
        
            # process chunks, if no chunks - previous data will be used
            if chunk_pn.shape[0] > 0:
                pn_buffer = self._process_chunk_pn(chunk_pn, pn_buffer)
            else:
                self._printm('empty pn chunk encountered')
                
            if chunk_amp.shape[0] > 0:
                #print(chunk_amp.shape)
                counter += chunk_amp.shape[0]
                WienerCoords, KalmanCoords = self._process_chunk_amp(chunk_amp)    
            
            # get predictions and factual result and send them to avatar
            prediction = KalmanCoords[-1,:len(self.pn_fingers_range)]   
            fact = np.copy(pn_buffer)
            self.coordbuff.append((prediction, fact))
            
            to_stimulate = (- 1.5*fact[0] - self.bias - 55 < 0) and (- fact[1] > 40)
            #print((- (1.2*fact[0]+15) - 55 > 0), (- fact[1] > 40))
            #print(to_stimulate)
            if self.stimulator is not None and to_stimulate:
                self._stimulate()    
            
            
            self._send_data_to_avatar(prediction, fact)
            
            # massage to see progress
            if counter // 3000 > counter_messages:
                counter_messages += 1
                self._printm('sent {} samples to avatar'.format(counter))
        
            # control time of each cycle of the loop 
            difference_time = self.avatar_period - (time.time() - cycle_start_time)
            if difference_time > 0:
                time.sleep(difference_time)
            else:
                self._printm('not enough time for chunks {} and {} processing, latency is {} s'.format(chunk_pn.shape, 
                                                                                                       chunk_amp.shape, 
                                                                                                       difference_time))
                self._printm('Kalman decoding time: {}'.format(self.kalman_time))
    
    
    def get_coordbuff(self):
        return self.coordbuff
    
    
    
    def _process_chunk_pn(self, chunk_pn, pn_buffer):
        chunk = chunk_pn[:, self.pn_fingers_range]
        medians = np.nanmedian(chunk, axis=0)
        pn_buffer[~np.isnan(medians)] = medians[~np.isnan(medians)]
        return pn_buffer
    
    def _process_chunk_amp(self, chunk_amp):
        chunk = chunk_amp[:, :self.numCh]
        chunk = self.filter.filterEMG(chunk)
        t4 = time.time()
        WienerCoords, KalmanCoords = self.decoder.transform(chunk)
        self.kalman_time = time.time() - t4
        return WienerCoords, KalmanCoords
        
    def _send_data_to_avatar(self, prediction, fact):
        fact[0] = fact[0] *1.5 + self.bias
        #fact[1] = fact[1] *1.2
        
        self.avatar_buffer[self.avatar_fingers_range] = fact
        self.avatar_buffer[self.avatar_fingers_range + self.avatar_buffer_size//2] = prediction
        
        data = struct.pack('%df' % len(self.avatar_buffer), *list(map(float, self.avatar_buffer)))
        self.sock.sendto(data, self.client_address)
        
    def _stimulate(self):
        if (time.time() - self.refractory_start)*1000 >= self.refractory_time:
            self.stimulator.stimulate(self.stimulation_time, 1)
            self.refractory_start = time.time()
        
        
        
    def _printm(self, message):
        print('{} {}: '.format(time.strftime('%H:%M:%S'), type(self).__name__) + message)

    
        