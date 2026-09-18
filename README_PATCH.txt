JP Planner v6.7 - slimme locatieherkenning

Overschrijf in GitHub:
- location_service.py
- ors_client.py

Nieuw in v6.7:
- gebruikt api.heigit.org voor OpenRouteService/Pelias;
- herkent locatienamen zoals "Beachclub Lemmer" en "De Beren Emmen" als zoekopdracht;
- als een bedrijfsnaam voor een echt adres staat, probeert de planner ook het adres apart;
- postcode + straat + plaats krijgen extra gewicht bij de keuze van een resultaat;
- meerdere geocoder-resultaten worden gescoord op naam, plaats, postcode en overeenkomst;
- gevonden coördinaten blijven in de bestaande databasecache bewaard.
