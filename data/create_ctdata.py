#! /usr/bin/env python

import sys, os, time, random, shutil
import numpy as np
import lmdb, caffe, nibabel 
import config
from multiprocessing import Pool, Process
import scipy.misc, scipy.ndimage.interpolation
from tqdm import tqdm 
import plyvel
from itertools import izip

N_PROC = config.N_PROC
IMG_DTYPE = np.float
SEG_DTYPE = np.uint8



def to_scale(img, shape=None):
	if shape is None:
		shape = config.slice_shape
		
	height, width = shape
	if img.dtype == SEG_DTYPE:
		return scipy.misc.imresize(img,(height,width),interp="nearest").astype(SEG_DTYPE)
	elif img.dtype == IMG_DTYPE:
		factor = 256.0/np.max(img)
		return (scipy.misc.imresize(img,(height,width),interp="nearest")/factor).astype(IMG_DTYPE)
	else:
		raise TypeError('Error. To scale the image array, its type must be np.uint8 or np.float64. (' + str(img.dtype) + ')')

def get_shift(img, seg, x, y):
	"""Move pixel in a direction by attaching on the other side. (i.e. x=5 -> 5 pixel to the right; y=-7 seven pixel down)
	:param id: slice id in current volume
	:return: Shifted img and seg"""
	# slide in x direction
	if x != 0:
		img = np.append(img[x:,:], img[:x,:], axis=0)
		seg = np.append(seg[x:,:], seg[:x,:], axis=0)
	# slide in y direction
	if y != 0:
		img = np.append(img[:,-y:], img[:,:-y], axis=1)
		seg = np.append(seg[:,-y:], seg[:,:-y], axis=1)
	return img, seg
	
def pad(img, pad_type, shape):
	""" Pads the given image to reach the desired shape.
	pad_type: 
	'lt': left top
	'rt': right top
	'lb': left bottom
	'rb': right bottom"""
	if shape is None:
		shape = config.slice_shape
	
	assert img.shape[0] <= shape[0], "Given img shape for padding: "+str(img.shape)+" longer than desired shape: "+str(shape)
	assert img.shape[1] <= shape[1], "Given img shape for padding: "+str(img.shape)+" longer than desired shape: "+str(shape)
	assert pad_type in ['lt','rt','lb','rb']
	y_pad, x_pad = shape[0] - img.shape[0], shape[1] - img.shape[1]
	
	before_y, after_y, before_x, after_x = 0,0,0,0
	if pad_type[0] == 'l': #left
		before_x = x_pad
	elif pad_type[0] == 'r':
		after_x = x_pad
	
	if pad_type[1] == 't': #top
		before_y = y_pad
	elif pad_type[1] == 'b':
		after_y = y_pad
	
	return np.pad(img, ((before_y, after_y),(before_x,after_x)), mode='constant')
	
	
def crop(img, seg, crop_type, frac=0.95):

	height, width = config.slice_shape
	
	if crop_type == 'lt':
		box = (0                       , 0,
			   int(round(width * frac)), int(round(height * frac)))
	elif crop_type == 'rt':
		box = (int(round((1.0 - frac) * width)), 0,
			   width                           , int(round(height * frac)))
	elif crop_type == 'lb':
		box = (0                       , int(round((1.0 - frac) * height)),
			   int(round(width * frac)), height)
	elif crop_type == 'rb':
		box = (int(round((1.0 - frac) * width)),int(round((1.0 - frac) * height)),
			   width,                           height)
	elif crop_type == 'c':
		box = (int(round((1.0 - frac) * (width/2.0))),       int(round((1.0 - frac) * (height/2.0))),
			   int(round(width * (frac + (1 - frac) / 2.0))),int(round(height * (frac + (1 - frac) / 2.0))))
	else:
		raise ValueError("Wrong crop_type. Must be lt, rt, lb, rb or c.")
	# Do the cropping
	x1, y1, x2, y2 = box
	img, seg = img[y1:y2, x1:x2], seg[y1:y2, x1:x2]
	# Return results
	img, seg = to_scale(img) , to_scale(seg)
	return img, seg

def rotate(img, angle):
	# Prevent augmentation with no rotation, otherwise the same image will be appended
	if angle==0:
		angle=1
	# rotate without interpolation (order=0 makes it take nearest pixel)
	rotated = scipy.ndimage.interpolation.rotate(img, angle, order=0)
	#rotation results in extra pixels on the borders
	# We fix it assuming square shape
	assert img.shape[0] == img.shape[1], "Given image for rotation is not of square shape :" + str(img.shape)
	extra = rotated.shape[0]-img.shape[0]
	extra_left = extra/2
	extra_right = extra - extra_left
	rotated = rotated[extra_left: -extra_right, extra_left: - extra_right]
	return rotated

def augment(img, seg, factor=None):
	"""
	Augment image by factor.
	:param img: img as 2d array
	:param seg: segmentation as 2d array
	:param factor: augmentation factor
	:return: volume of images, volume of segmentations
	"""
	if factor is None:
		factor = config.augmentation_factor
		
	# number of available augmentation functions
	max_fac = 50
	#initialize with unaltered slices
	imgs, segs = [img], [seg] 

	if factor > max_fac:
		raise ValueError("Max possible augmentation factor: " + str(max_fac))
	height, width = config.slice_shape
	# init selectors and start augmentation loop
	for selector in xrange(factor+1):
		if selector < 2:
			continue
		elif selector == 2:# mirror x
			img_, seg_ = np.fliplr(img), np.fliplr(seg)
			imgs.append(img_);segs.append(seg_)
		elif selector == 3:# mirror y
			img_, seg_ = np.flipud(img), np.flipud(seg)
			imgs.append(img_);segs.append(seg_)
		elif selector == 4:# turn 90
			img_, seg_ = np.rot90(img, 1), np.rot90(seg, 1)
			imgs.append(img_);segs.append(seg_)
		elif selector == 5:# turn 180
			img_, seg_ = np.rot90(img, 2), np.rot90(seg, 2)
			imgs.append(img_);segs.append(seg_)
		elif selector == 6:# turn 270
			img_, seg_ = np.rot90(img, 3), np.rot90(seg, 3)
			imgs.append(img_);segs.append(seg_)
		elif selector == 7:# noise
			img_noisy = (img + 0.7 * img.std() * np.random.random(img.shape)).astype(IMG_DTYPE)
			img_, seg_ = img_noisy, seg
			imgs.append(img_);segs.append(seg_)
		elif selector == 8:# crop lb
			img_, seg_ = crop(img, seg, 'lb')
			imgs.append(img_);segs.append(seg_)
		elif selector == 9:# crop rt
			img_, seg_ = crop(img, seg, 'rt')
			imgs.append(img_);segs.append(seg_)
		elif selector == 10:# crop c
			img_, seg_ = crop(img, seg, 'c')
			imgs.append(img_);segs.append(seg_)
		elif selector == 11:# random rotation by [-10, 10] degrees
			rand = random.randrange(-10,10)
			img_, seg_ = rotate(img, rand), rotate(seg, rand) 
			imgs.append(img_);segs.append(seg_)
		elif selector == 12:# random rotation by [-10, 10] degrees
			rand = random.randrange(-10,10)
			img_, seg_ = rotate(img, rand), rotate(seg, rand) 
			imgs.append(img_);segs.append(seg_)
		elif selector == 13:# shift down
			img_, seg_ = get_shift(img, seg, 0, -int(height/15))
			imgs.append(img_);segs.append(seg_)
		elif selector == 14:# shift up
			img_, seg_ = get_shift(img, seg, 0, int(height/15))
			imgs.append(img_);segs.append(seg_)
		elif selector == 15:# shift right
			img_, seg_ = get_shift(img, seg, int(width/15), 0)
			imgs.append(img_);segs.append(seg_)
		elif selector == 16:# shift left
			img_, seg_ = get_shift(img, seg, -int(width/15), 0)
			imgs.append(img_);segs.append(seg_)
		elif selector == 17:# shift right up
			img_, seg_ = get_shift(img, seg, int(width/15), int(height/15))
			imgs.append(img_);segs.append(seg_)
		elif selector == 18:# shift left down
			img_, seg_ = get_shift(img, seg, -int(width/15), -int(height/15))
			imgs.append(img_);segs.append(seg_)
		elif selector == 19:# shift left up
			img_, seg_ = get_shift(img, seg, -int(width/15), int(height/15))
			imgs.append(img_);segs.append(seg_)
		elif selector == 20:# shift right down
			img_, seg_ = get_shift(img, seg, int(width/15), -int(height/15))
			imgs.append(img_);segs.append(seg_)
		elif selector == 21:# crop lt
			img_, seg_ = crop(img,seg, 'lt')
			imgs.append(img_);segs.append(seg_)
		elif selector == 22:# crop rb
			img_, seg_ = crop(img,seg, 'rb')
			imgs.append(img_);segs.append(seg_)
		elif selector > 22 and selector <= 50:
			rand = random.randrange(-75,75)
			img_, seg_ = rotate(img, rand), rotate(seg, rand) 
			imgs.append(img_);segs.append(seg_)
		else:
			break

	#imgs,segs= np.array(imgs), np.array(segs)
	return imgs,segs


def norm_hounsfield_dyn(arr, c_min=0.1, c_max=0.3):
	""" Converts from hounsfield units to float64 image with range 0.0 to 1.0 """
	# calc min and max
	min,max = np.amin(arr), np.amax(arr)
	arr = arr.astype(IMG_DTYPE)
	if min <= 0:
		arr = np.clip(arr, min * c_min, max * c_max)
		# right shift to zero
		arr = np.abs(min * c_min) + arr
	else:
		arr = np.clip(arr, min, max * c_max)
		# left shift to zero
		arr = arr - min
	# normalization
	norm_fac = np.amax(arr)
	if norm_fac != 0:
		norm = np.divide(
				np.multiply(arr,255),
			 	np.amax(arr))
	else:  # don't divide through 0
		norm = np.multiply(arr, 255)
		
	norm = np.clip(np.multiply(norm, 0.00390625), 0, 1)
	return norm


def create_lmdb_keys(uid_sliceidx):
	""" Creates lmdb keys for img and label slices """
	uid, slice_idx = uid_sliceidx
	if config.shuffle_slices==True:
		random = np.random.random()*100000
	else:
		random = 0
	key_seg = '%05i_%08d_%05d_seg' % (random, slice_idx, uid)
	key_img = '%05i_%08d_%05d_img' % (random, slice_idx, uid)
	return key_img, key_seg


def is_relevant_slice(slc):
	""" Checks whether a given slice is relevant, according to rule specified in config.select_slices (e.g., lesion-only)"""
	max = np.max(slc)
	if config.select_slices == "liver-lesion":
		return max == 1 or max == 2
	elif config.select_slices == "lesion-only":
		return max == 2
	elif config.select_slices == "liver-only":
		return max == 1
	else:
		raise ValueError("Invalid value for config.select_slices :", config.select_slices)



def histeq_processor(img, seg):
	"""Histogram equalization"""
	nbr_bins=256
	#get image histogram
	imhist,bins = np.histogram(img.flatten(),nbr_bins,normed=True)
	cdf = imhist.cumsum() #cumulative distribution function
	cdf = 255 * cdf / cdf[-1] #normalize
	#use linear interpolation of cdf to find new pixel values
	original_shape = img.shape
	img = np.interp(img.flatten(),bins[:-1],cdf)
	img=img/255.0
	return img.reshape(original_shape), seg

def plain_UNET_processor(img,seg):
	img = to_scale(img, (388,388))
	seg = to_scale(seg, (388,388))
	# Now do padding for UNET, which takes 572x572
	#seg=np.pad(seg,((92,92),(92,92)),mode='reflect')
	img=np.pad(img,92,mode='reflect')
	return img, seg

def zoomliver_UNET_processor(img, seg):
	""" Custom preprocessing of img,seg for UNET architecture:
	Crops the background and upsamples the found patch."""
	
	# Remove background !
	img = np.multiply(img,np.clip(seg,0,1))
	# get patch size
	col_maxes = np.max(seg, axis=0) # a row
	row_maxes = np.max(seg, axis=1)# a column
	
	nonzero_colmaxes = np.nonzero(col_maxes)[0]
	nonzero_rowmaxes = np.nonzero(row_maxes)[0]
	
	x1, x2 = nonzero_colmaxes[0], nonzero_colmaxes[-1]
	y1, y2 = nonzero_rowmaxes[0], nonzero_rowmaxes[-1]
	width = x2-x1
	height= y2-y1
	MIN_WIDTH = 60
	MIN_HEIGHT= 60
	x_pad = (MIN_WIDTH - width) / 2.0 if width < MIN_WIDTH else 0
	y_pad = (MIN_HEIGHT - height)/2.0 if height < MIN_HEIGHT else 0
	
	x1 = max(0, x1-x_pad)
	x2 = min(img.shape[1], x2+x_pad)
	y1 = max(0, y1-y_pad)
	y2 = min(img.shape[0], y2+y_pad)  
	
	img = img[y1:y2+1, x1:x2+1]
	seg = seg[y1:y2+1, x1:x2+1]
	
	img = to_scale(img, (388,388))
	seg = to_scale(seg, (388,388))
	# All non-lesion is background
	seg[seg==1]=0
	#Lesion label becomes 1
	seg[seg==2]=1
	
	# Now do padding for UNET, which takes 572x572
	#seg=np.pad(seg,((92,92),(92,92)),mode='reflect')
	img=np.pad(img,92,mode='reflect')
	return img, seg



def process_img_slice(img_seg):
	""" Process img and seg, and augment them. Return tuple (volume of imgs, volume of segs) 
	The volume returned is the given image with augmentation, all as a volume (np 3D array)"""
	img, seg = img_seg
	# Process Image 	
	img = norm_hounsfield_dyn(img)
	img = to_scale(img)
	# Process Seg
	seg = np.clip(seg, 0, 2).astype(SEG_DTYPE)
	seg = to_scale(seg)
	
	imgs,segs = augment(img, seg)
	assert len(imgs)==len(segs), "Augmentation yielded different number of images and segmentations: "+str(len(imgs))+" vs "+str(len(segs))
	# Pre-write (final) processing
	for i in range(len(imgs)):
		for processor in config.processors_list:
			imgs[i], segs[i] = processor(imgs[i], segs[i])
	
	return imgs, segs

def serialize(arr):
	arr = arr.reshape(1, arr.shape[0], arr.shape[1]) # add channel dimension
	datum_serialized = caffe.io.array_to_datum(arr).SerializeToString()
	return datum_serialized


def process_volume(uid, volume_file, seg_file):
	""" Takes an img and seg volume filepaths and volume uid.
	Loads the 2 niftis
	Processes the slices in both volumes
	Puts them in datums
	Returns keys and values for both img and seg databases"""
	
	ppool=Pool(N_PROC)
	#print "Reading volume uid",uid
	# LOAD NIFTIS
	volume = nibabel.load(volume_file).get_data()
	volume = np.rot90(volume)
	volume = np.transpose(volume, (2,0,1)) #make first index for slice index
	segmentation = nibabel.load(seg_file).get_data()
	segmentation = np.rot90(segmentation)
	segmentation = np.transpose(segmentation, (2,0,1))
	assert volume.shape == segmentation.shape, "Volume and segmentation have different shapes: %s vs. %s" % (str(volume.shape),str(segmentation.shape))

	#print "Filtering volume uid",uid
	# FILTER RELEVANT SLICES
	# Determine indices of relevant slices (in parallel)
	#print segmentation.shape
	idx_relevant_slices = np.where(ppool.map(is_relevant_slice, segmentation))[0]
	# Take only relevant slices
	volume = volume[idx_relevant_slices]
	segmentation = segmentation[idx_relevant_slices]

	#print "Process/Augment volume uid", uid
	# PROCESS SLICES
	# Given list of (img,seg) tuples, returns list of (img volume, seg volume) tuples
	# Each slice becomes a volume "due to augmentation"
	list_of_imgsvol_segsvol= ppool.map(process_img_slice, izip(volume,segmentation))
	ppool.close()
	
	#print 'Zipping..'
	# now make it a tuple (list of img volumes, list of seg volumes)
	imgvols, segvols = izip(*list_of_imgsvol_segsvol) #e.g., imvgols is a list of (17,388,388) arrays
	# then make it one large img volume, and one large seg volume (volume having all original images and their augmentations)
	# so the resulting volume's first dimension = original number of slices * augmentation factor
	#for i in imgvols: print i.shape
	#print 'Concatenating...'
	volume       = np.concatenate(imgvols, axis=0) # join all volumes into one big volume
	segmentation = np.concatenate(segvols, axis=0)

	# CREATE LMDB KEYS FOR ALL SLICES IN IMG AND SEG
	n_slices = volume.shape[0]
	uids=[uid]*n_slices
	slice_idx = range(n_slices)
	#print len(uids),len(slice_idx)
	keyimg_keyseg = map(create_lmdb_keys, izip( uids, slice_idx))
	keys_img, keys_seg = izip(*keyimg_keyseg)
	return volume, segmentation, keys_img, keys_seg


def persist_volumes(uid, imgvol, segvol, keys_img, keys_seg):
	""" Writes slices of given volume into lmdb database. uid is just a unique id of the volume"""
	ppool=Pool(N_PROC)
	
	dbsize = 50*1024**3 # 50 GB max size
	def persist_volume(volume, keys, dbpath):
		assert volume.shape[0] == len(keys), "Number of keys and number of slices mismatch:"+str(volume.shape)+" , # keys: "+str(len(keys))

		#print 'Persisting ', uid, 'to', dbpath
		env = lmdb.open(dbpath, map_size=dbsize, sync=True) # returns environment
		
		batch_size = 500
		start_ = 0
		end_   = batch_size
		while True:
			txn = env.begin(write=True)
			dbwriter = txn.cursor()
			# Create small batch
			mini_volume = volume[start_:end_]
			mini_keys   = keys[start_:end_]
			
			if start_ >= volume.shape[0]:
				break
			#print 'Serializing',uid,' from',start_, 'to', end_
			datums = ppool.map(serialize, mini_volume)
			#print 'writing'
			dbwriter.putmulti(izip(mini_keys, datums))
			txn.commit()
			
			start_ += batch_size
			end_   += batch_size
			
		env.close()
		#print 'Done committing volume uid ',uid, 'to',dbpath
	
	persist_volume(segvol, keys_seg, segdb_path)
	persist_volume(imgvol, keys_img, imgdb_path)
	ppool.close()
	
if __name__ == '__main__':
	# Create parent directory
	if not os.path.exists(config.lmdb_path):
		os.makedirs(config.lmdb_path)
	imgdb_path = os.path.join(config.lmdb_path,"img")
	segdb_path = os.path.join(config.lmdb_path,"seg")
	assert not os.path.exists(imgdb_path), "Image Database path exists :"+ imgdb_path
	assert not os.path.exists(segdb_path), "Seg Database path exists :"+ segdb_path
	# Copy config file as metadata with the generated dbs
	config_path = os.path.join(os.path.dirname(__file__),"config.py")
	shutil.copy(config_path, config.lmdb_path)
	copied_path = os.path.join(config.lmdb_path, "config.py")
	os.chmod(copied_path, 292) #chmod 444
	
	
	dataset = config.dataset if config.max_volumes < 0 else config.dataset[:config.max_volumes]
	
	p = None
	for uid, volume_file, seg_file in tqdm(dataset):
		imgvol, segvol, keys_img, keys_seg = process_volume(uid, volume_file, seg_file)
		# Wait for previous persist_volumes process
		if p is not None:
			p.join()
		p = Process(target=persist_volumes, args=(uid, imgvol, segvol, keys_img, keys_seg))
		p.start()
			
	print "All Done.."




