-- Drop city expansion: real restaurants for 10 US cities.
-- All entries are well-known, real establishments. Prices/menus not included;
-- Ken must verify prices before real orders. Phone numbers are best-effort and
-- should be verified via a quick web check before going live.
-- Safe to re-run: INSERTs only, no schema changes.

-- ================= ATLANTA GA =================
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('The Varsity', 'American · Burgers', '61 North Ave NW', 'Atlanta', 'GA', '(404) 881-1706', '🍔');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Mary Mac''s Tea Room', 'Southern · Comfort', '224 Ponce De Leon Ave NE', 'Atlanta', 'GA', '(404) 876-1800', '🍗');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('The Vortex Bar & Grill', 'Burgers · American', '438 Moreland Ave NE', 'Atlanta', 'GA', '(404) 688-1828', '🍔');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Fox Bros Bar-B-Q', 'BBQ', '1238 DeKalb Ave NE', 'Atlanta', 'GA', '(404) 577-4030', '🍖');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Antico Pizza Napoletana', 'Pizza · Italian', '1093 Hemphill Ave NW', 'Atlanta', 'GA', '(404) 724-2333', '🍕');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('The Busy Bee Cafe', 'Soul Food', '810 Martin Luther King Jr Dr SW', 'Atlanta', 'GA', '(404) 521-9061', '🍗');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Slutty Vegan', 'Vegan · Burgers', '1542 Ralph David Abernathy Blvd SW', 'Atlanta', 'GA', '(470) 240-1976', '🌱');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Canoe', 'American · Fine Dining', '4199 Paces Ferry Rd SE', 'Atlanta', 'GA', '(770) 432-2663', '🍽️');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Bacchanalia', 'Fine Dining · American', '1460 Ellsworth Industrial Blvd NW', 'Atlanta', 'GA', '(404) 365-0410', '🍽️');

-- ================= DALLAS TX =================
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Pecan Lodge', 'BBQ', '2702 Main St', 'Dallas', 'TX', '(214) 748-8900', '🍖');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Velvet Taco', 'Tacos', '3012 Gulden Ln', 'Dallas', 'TX', '', '🌮');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Mi Cocina', 'Tex-Mex', '2515 McKinney Ave', 'Dallas', 'TX', '(214) 754-0368', '🌮');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Hutchins BBQ', 'BBQ', '1301 N Tennessee St', 'McKinney', 'TX', '(214) 548-4119', '🍖');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Terry Black''s Barbecue', 'BBQ', '3025 Main St', 'Dallas', 'TX', '(214) 997-1551', '🍖');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Uchi Dallas', 'Japanese · Sushi', '2817 Maple Ave', 'Dallas', 'TX', '(214) 855-5454', '🍣');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Cane Rosso', 'Pizza · Italian', '2612 Commerce St', 'Dallas', 'TX', '(214) 741-4760', '🍕');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Ellen''s', 'Breakfast · Brunch', '1790 N Record St', 'Dallas', 'TX', '(214) 702-4667', '🥞');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Rodeo Goat', 'Burgers', '1926 Market Center Blvd', 'Dallas', 'TX', '(214) 741-4628', '🍔');

-- ================= HOUSTON TX =================
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('The Breakfast Klub', 'Breakfast', '3711 Travis St', 'Houston', 'TX', '(713) 528-8561', '🥞');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Pappadeaux Seafood Kitchen', 'Seafood · Cajun', '2410 Richmond Ave', 'Houston', 'TX', '(713) 521-2233', '🦐');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Killen''s Barbecue', 'BBQ', '3613 E Broadway St', 'Pearland', 'TX', '(281) 485-2272', '🍖');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Torchy''s Tacos', 'Tacos', '2809 S Shepherd Dr', 'Houston', 'TX', '', '🌮');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Uchi Houston', 'Japanese · Sushi', '904 Westheimer Rd', 'Houston', 'TX', '(713) 522-4808', '🍣');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Goode Company BBQ', 'BBQ', '5109 Kirby Dr', 'Houston', 'TX', '(713) 522-2530', '🍖');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('The Original Ninfa''s', 'Tex-Mex', '2704 Navigation Blvd', 'Houston', 'TX', '(713) 228-1175', '🌮');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Crawfish & Noodles', 'Vietnamese-Cajun · Seafood', '11360 Bellaire Blvd #990', 'Houston', 'TX', '(281) 988-8098', '🦐');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Lankford Grocery', 'Burgers', '88 Dennis St', 'Houston', 'TX', '(713) 522-9555', '🍔');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Pinkerton''s Barbecue', 'BBQ', '1100 E 5 1/2 St', 'Houston', 'TX', '(713) 904-9100', '🍖');

-- ================= CHICAGO IL =================
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Lou Malnati''s Pizzeria', 'Deep Dish Pizza', '439 N Wells St', 'Chicago', 'IL', '(312) 828-9800', '🍕');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Giordano''s', 'Deep Dish Pizza', '730 N Rush St', 'Chicago', 'IL', '(312) 951-0747', '🍕');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Portillo''s Hot Dogs', 'Hot Dogs · Italian Beef', '100 W Ontario St', 'Chicago', 'IL', '(312) 587-8910', '🌭');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Al''s #1 Italian Beef', 'Italian Beef', '1079 W Taylor St', 'Chicago', 'IL', '(312) 226-4017', '🥪');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Girl & the Goat', 'American', '809 W Randolph St', 'Chicago', 'IL', '(312) 492-6262', '🍽️');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Pequod''s Pizza', 'Pan Pizza', '2207 N Clybourn Ave', 'Chicago', 'IL', '(773) 327-1512', '🍕');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('The Wiener''s Circle', 'Hot Dogs', '2622 N Clark St', 'Chicago', 'IL', '(773) 477-7444', '🌭');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Mr. Beef', 'Italian Beef', '666 N Orleans St', 'Chicago', 'IL', '(312) 337-8500', '🥪');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Xoco', 'Mexican', '449 N Clark St', 'Chicago', 'IL', '(312) 334-3688', '🌮');

-- ================= LOS ANGELES CA =================
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('In-N-Out Burger', 'Burgers', '9149 S Sepulveda Blvd', 'Los Angeles', 'CA', '(800) 786-1000', '🍔');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Pink''s Hot Dogs', 'Hot Dogs', '709 N La Brea Ave', 'Los Angeles', 'CA', '(323) 931-4223', '🌭');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('The Original Pantry Cafe', 'Diner · American', '877 S Figueroa St', 'Los Angeles', 'CA', '(213) 972-9279', '🥞');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Philippe The Original', 'Deli · French Dip', '1001 N Alameda St', 'Los Angeles', 'CA', '(213) 628-3781', '🥪');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Guisados', 'Tacos', '2100 E Cesar E Chavez Ave', 'Los Angeles', 'CA', '(323) 264-7201', '🌮');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Howlin'' Ray''s', 'Nashville Hot Chicken', '727 N Broadway #128', 'Los Angeles', 'CA', '(213) 935-8399', '🍗');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Roscoe''s House of Chicken & Waffles', 'Soul Food', '1514 N Gower St', 'Los Angeles', 'CA', '(323) 466-7453', '🧇');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Langer''s Delicatessen', 'Deli', '704 S Alvarado St', 'Los Angeles', 'CA', '(213) 483-8050', '🥪');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Bestia', 'Italian', '2121 E 7th Pl', 'Los Angeles', 'CA', '(213) 514-5724', '🍽️');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Jon & Vinny''s', 'Italian', '412 N Fairfax Ave', 'Los Angeles', 'CA', '(323) 334-3369', '🍕');

-- ================= NEW YORK NY =================
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Katz''s Delicatessen', 'Deli', '205 E Houston St', 'New York', 'NY', '(212) 254-2246', '🥪');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Joe''s Pizza', 'Pizza', '7 Carmine St', 'New York', 'NY', '(212) 366-1182', '🍕');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Shake Shack', 'Burgers', '11 Madison Ave', 'New York', 'NY', '', '🍔');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Xi''an Famous Foods', 'Chinese · Noodles', '45 Bayard St', 'New York', 'NY', '(212) 786-2068', '🍜');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Los Tacos No. 1', 'Tacos', '75 9th Ave', 'New York', 'NY', '', '🌮');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Prince Street Pizza', 'Pizza', '27 Prince St', 'New York', 'NY', '(212) 966-4100', '🍕');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Russ & Daughters', 'Bagels · Appetizing', '179 E Houston St', 'New York', 'NY', '(212) 475-4880', '🥯');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Di Fara Pizza', 'Pizza', '1424 Avenue J', 'Brooklyn', 'NY', '(718) 258-1367', '🍕');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Peter Luger Steak House', 'Steakhouse', '178 Broadway', 'Brooklyn', 'NY', '(718) 387-7400', '🥩');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Rubirosa', 'Italian · Pizza', '235 Mulberry St', 'New York', 'NY', '(212) 965-1700', '🍕');

-- ================= MIAMI FL =================
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Versailles Restaurant', 'Cuban', '3555 SW 8th St', 'Miami', 'FL', '(305) 444-0240', '🍖');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Joe''s Stone Crab', 'Seafood', '11 Washington Ave', 'Miami Beach', 'FL', '(305) 673-0365', '🦀');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('La Sandwicherie', 'Sandwiches', '229 14th St', 'Miami Beach', 'FL', '(305) 532-8934', '🥪');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Coyo Taco', 'Tacos', '2300 NW 2nd Ave', 'Miami', 'FL', '', '🌮');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Enriqueta''s Sandwich Shop', 'Cuban Sandwiches', '186 NE 29th St', 'Miami', 'FL', '(305) 573-4681', '🥪');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Garcia''s Seafood Grille', 'Seafood', '398 NW North River Dr', 'Miami', 'FL', '(305) 375-0765', '🦐');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Puerto Sagua', 'Cuban', '700 Collins Ave', 'Miami Beach', 'FL', '(305) 673-1115', '🍗');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('El Palacio De Los Jugos', 'Cuban · Juice Bar', '5721 W Flagler St', 'Miami', 'FL', '', '🥤');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Sanguich De Miami', 'Cuban Sandwiches', '1042 SW 8th St', 'Miami', 'FL', '(786) 558-8368', '🥪');

-- ================= DENVER CO =================
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Casa Bonita', 'Mexican', '6715 W Colfax Ave', 'Lakewood', 'CO', '(303) 232-5115', '🌮');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Sam''s No. 3', 'Diner', '1500 Curtis St', 'Denver', 'CO', '(303) 534-2227', '🥞');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Biker Jim''s Gourmet Dogs', 'Hot Dogs', '2148 Larimer St', 'Denver', 'CO', '(303) 209-2808', '🌭');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Denver Biscuit Company', 'Biscuits · Breakfast', '3237 E Colfax Ave', 'Denver', 'CO', '(303) 377-7900', '🥐');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Illegal Pete''s', 'Burritos', '270 S Broadway', 'Denver', 'CO', '(303) 778-1397', '🌯');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Snooze, an A.M. Eatery', 'Breakfast', '2262 Larimer St', 'Denver', 'CO', '(303) 297-0700', '🥞');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Tacos Tequila Whiskey', 'Tacos', '3300 Blake St', 'Denver', 'CO', '(303) 297-4939', '🌮');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('The Buckhorn Exchange', 'Steakhouse', '1000 Osage St', 'Denver', 'CO', '(303) 534-9505', '🥩');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Rosenberg''s Bagels', 'Bagels · Deli', '725 E 26th Ave', 'Denver', 'CO', '(720) 519-3841', '🥯');

-- ================= SEATTLE WA =================
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Pike Place Chowder', 'Seafood · Chowder', '1530 Post Alley', 'Seattle', 'WA', '(206) 267-2537', '🦐');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Dick''s Drive-In', 'Burgers', '111 NE 45th St', 'Seattle', 'WA', '(206) 632-5125', '🍔');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Piroshky Piroshky', 'Russian Bakery', '1908 Pike Pl', 'Seattle', 'WA', '(206) 441-6068', '🥐');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Salumi', 'Italian Deli', '309 3rd Ave S', 'Seattle', 'WA', '(206) 621-8772', '🥪');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('The Pink Door', 'Italian', '1919 Post Alley', 'Seattle', 'WA', '(206) 443-3241', '🍝');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Din Tai Fung', 'Chinese · Dumplings', '2621 NE 46th St', 'Seattle', 'WA', '(206) 525-0958', '🥟');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Serious Pie', 'Pizza', '316 Virginia St', 'Seattle', 'WA', '(206) 838-7388', '🍕');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Ivar''s Acres of Clams', 'Seafood', '1001 Alaskan Way', 'Seattle', 'WA', '(206) 624-6852', '🦐');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Marination Ma Kai', 'Hawaiian', '1660 Harbor Ave SW', 'Seattle', 'WA', '(206) 328-8226', '🌺');

-- ================= PHOENIX AZ =================
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Pizzeria Bianco', 'Pizza', '623 E Adams St', 'Phoenix', 'AZ', '(602) 258-8300', '🍕');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Matt''s Big Breakfast', 'Breakfast', '825 N 1st St', 'Phoenix', 'AZ', '(602) 254-1074', '🥞');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Los Dos Molinos', 'Mexican', '8646 S Central Ave', 'Phoenix', 'AZ', '(602) 243-9113', '🌮');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Carolina''s Mexican Food', 'Mexican', '2126 E Cactus Rd', 'Phoenix', 'AZ', '(602) 749-5961', '🌮');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Little Miss BBQ', 'BBQ', '4301 E University Dr', 'Phoenix', 'AZ', '(602) 437-1177', '🍖');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Welcome Diner', 'Diner', '924 E Roosevelt St', 'Phoenix', 'AZ', '(602) 495-1114', '🍔');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Tacos Huicho', 'Tacos', '822 N 16th St', 'Phoenix', 'AZ', '(602) 322-8824', '🌮');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Chino Bandido', 'Chinese-Mexican Fusion', '15414 N 19th Ave', 'Phoenix', 'AZ', '(602) 375-3639', '🍜');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Barrio Cafe', 'Mexican', '2814 N 16th St', 'Phoenix', 'AZ', '(602) 636-0240', '🌮');
INSERT INTO restaurants (name, cuisine, address, city, state, phone, emoji) VALUES ('Durant''s', 'Steakhouse', '2611 N Central Ave', 'Phoenix', 'AZ', '(602) 264-5967', '🥩');
